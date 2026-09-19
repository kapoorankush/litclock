"""Tests for scripts/update.sh (issue litclock-dev#160).

Two layers of coverage:

1. Structural tests — grep the script for code patterns that MUST be present.
   These encode every past bug / invariant we care about: the --no-block
   systemd hack, the checksum-based self-reexec guard, GPIO race ordering,
   venv quote-tolerant grep, etc.

2. Execution sandbox tests — run update.sh against a fake install directory
   with PATH stubs for git/sudo/systemctl/python3/pip. Assert the script
   reaches the right phases and makes the expected subprocess calls.
"""

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def update_sh_content():
    return UPDATE_SH.read_text()


# ── Structural tests ──────────────────────────────────────────────────


def _tmpfiles_block(content):
    """The tmpfiles install block, delimited by real anchors.

    Was a fixed 2600-character slice, which is a magic number masquerading as a
    boundary: adding a comment inside the block silently pushed the tail out of
    the window and the assertions started reading truncated text. Anchor on the
    counter that opens the block and the next section that follows it.
    """
    start = content.find("tmpfiles_installed=0")
    assert start != -1, "tmpfiles install counter missing"
    end = content.find("if [[ ${#ENABLED_UNITS[@]}", start)
    assert end != -1, "could not find the section following the tmpfiles block"
    return content[start:end]


class TestUpdateScriptStructure:
    """Grep-based invariants for update.sh."""

    def test_timer_start_uses_no_block(self, update_sh_content):
        """`systemctl start litclock.timer` from within a service deadlocks
        without --no-block. update.sh is often invoked from a systemd unit.

        Actually: update.sh is run interactively by the user, but the pattern
        of starting units from a script context still benefits from non-blocking
        semantics — and we want a guard against regressing to blocking start
        if update.sh ever gets wired into a service.

        Post-litclock-dev#209: the smoke-test failure path (Phase 4.5) also restarts the
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
        """litclock-dev#249/litclock-dev#251: enabling a unit registers it with systemd but does not
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
        """litclock-dev#251 explicit ask: 'only timers, not services — services should
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

    def test_tmpfiles_dropins_are_globbed_not_named(self, update_sh_content):
        """litclock-dev#547's shape on the OTA path. This was
        `if [[ -f .../tmpfiles.d/litclock.conf ]]; then sudo cp ...` — a
        hand-maintained name behind a soft `if`, so a second drop-in would
        never reach a fielded Pi and a missing one skipped with no journal
        line. pi-gen/stage3/03-install-services globs; so must this."""
        assert 'for conf in "$INSTALL_DIR"/systemd/tmpfiles.d/*.conf' in update_sh_content, (
            "update.sh must glob systemd/tmpfiles.d/ rather than naming litclock.conf"
        )
        hardcoded = re.findall(
            r"^\s*(?:sudo\s+)?cp\b[^\n]*?/systemd/tmpfiles\.d/[\w.\-]+\.conf",
            update_sh_content,
            re.MULTILINE,
        )
        assert not hardcoded, f"per-file cp of a tmpfiles drop-in reintroduced: {hardcoded}"

    def test_missing_tmpfiles_dropins_warn_rather_than_skip_silently(self, update_sh_content):
        """A glob that matches nothing exits 0. The old soft `if` reported
        nothing in that case; an OTA that silently stops refreshing the rules
        for /run/litclock and /var/lib/litclock must at least say so."""
        block = _tmpfiles_block(update_sh_content)
        assert re.search(r"if \[\[ \$tmpfiles_seen -eq 0 \]\]", block), "no zero-drop-in check after the tmpfiles glob"
        assert "log_warn" in block.split("tmpfiles_seen -eq 0")[1][:400], (
            "the zero-drop-in case must log_warn, not pass silently"
        )

    def test_tmpfiles_glob_is_paired_with_nullglob(self, update_sh_content):
        """Without `shopt -s nullglob` an unmatched glob stays LITERAL, so the
        copy fails on a path containing `*`, and — because update.sh has no
        errexit — the loop body still runs once. Verified defeat: deleting the
        shopt left the other tests green while making the zero-drop-in warning
        unreachable, i.e. the new guard reported success having installed
        nothing. Restore is save/restore, not a blind `shopt -u`, so a future
        `shopt -s nullglob` earlier in this 1400-line script is not silently
        cleared for every later phase."""
        # litclock-dev#782 — assert on EXECUTED lines. This block's comments
        # name `shopt -s nullglob` while explaining why it is needed, so a raw
        # membership test was satisfied by the prose: measured green with the
        # executed line deleted.
        block = _executed_lines(_tmpfiles_block(update_sh_content))
        assert "shopt -s nullglob" in block, "the tmpfiles glob is not guarded by nullglob"
        assert "_ng_saved=$(shopt -p nullglob)" in block, "nullglob must be saved before being set"
        assert 'eval "$_ng_saved"' in block, "nullglob must be RESTORED, not blindly cleared"

    def test_tmpfiles_copy_failure_is_checked_before_counting(self, update_sh_content):
        """The counter must not advance on a failed privileged copy.

        update.sh has no errexit, so an unchecked `cp` left the PREVIOUS
        release's drop-in in place, `systemd-tmpfiles --create` then succeeded
        against that stale file so its own `|| log_warn` never fired, the
        counter read non-zero so the zero-drop-in warning never fired either,
        and the OTA reported success having installed nothing — on the only path
        that touches already-working clocks.
        """
        block = _tmpfiles_block(update_sh_content)
        assert re.search(r"if ! sudo install -m 0644 -o root -g root \"\$conf\" /etc/tmpfiles\.d/; then", block), (
            "the tmpfiles copy must be failure-checked before the counter advances"
        )
        # The increment must come AFTER the guarded copy, not before it.
        copy_at = block.find("if ! sudo install -m 0644")
        inc_at = block.find("tmpfiles_installed=$(( tmpfiles_installed + 1 ))")
        assert copy_at != -1 and inc_at > copy_at, "counter increments before the copy is verified"

    def test_tmpfiles_and_units_are_installed_not_cp(self, update_sh_content):
        """`install -m 0644 -o root -g root`, matching pi-gen's 03. `cp` applies
        the SOURCE mode, so a drop-in committed 0755 or 0600 landed correctly on
        a flashed image and incorrectly on every OTA'd Pi — a divergence only
        reproducible on the fielded fleet, which is the one configuration QA
        never covers."""
        block = _tmpfiles_block(update_sh_content)
        assert not re.search(r"sudo cp \"\$conf\"", block), "tmpfiles drop-ins are still copied with plain cp"

    def test_tmpfiles_rejects_non_regular_files(self, update_sh_content):
        """$INSTALL_DIR is pi-writable and `cp`/`install` dereference the source,
        so a symlink here would make the sudo below read an arbitrary path and
        write it into /etc/tmpfiles.d/, which root systemd-tmpfiles parses at
        every boot. pi-gen's 03 rejects these explicitly; this path did not."""
        block = _tmpfiles_block(update_sh_content)
        assert re.search(r'\[\[ ! -f "\$conf" \|\| -L "\$conf" \]\]', block), (
            "no regular-file/symlink guard before the privileged tmpfiles copy"
        )

    def test_failed_enable_is_not_recorded_as_newly_enabled(self, update_sh_content):
        """ENABLED_UNITS drives the "Newly enabled" log line and the end-of-run
        summary. With no errexit, a failed `systemctl enable` used to fall
        through and still append, so the OTA reported a new timer as enabled
        while it stayed disabled — nothing runs, nothing complains."""
        assert re.search(
            r"if sudo systemctl enable \"\$name\"; then\s*\n\s*ENABLED_UNITS\+=\(\"\$name\"\)", update_sh_content
        ), "ENABLED_UNITS must only record units whose `systemctl enable` actually succeeded"

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
        """PR litclock-dev#94: update.sh re-execs itself if its own bytes changed mid-run,
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
        # litclock-dev#782 — EXECUTED lines: the docstring above and the
        # migration's own comments say "author-clock", so the raw form passed
        # with the migration deleted (measured).
        executed = _executed_lines(update_sh_content)
        assert "author-clock" in executed
        assert "/etc/authorclock" in executed
        assert "/etc/litclock" in executed
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
        # flags (e.g. --upgrade added in litclock-dev#321).
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
        # litclock-dev#782 — EXECUTED lines (3 executed lines could vanish green).
        executed = _executed_lines(update_sh_content)
        assert "env.sh.sample" in executed
        assert 'grep -q "^[# ]*export[[:space:]]\\+${varname}=" "$INSTALL_DIR/env.sh"' in executed

    def test_stale_symlinks_removed(self, update_sh_content):
        """Post-litclock-dev#79 reorg: root-level script symlinks (boot-splash.sh etc.)
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
        """litclock-dev#241 — Phase 7 must touch $POST_UPDATE_GRACE_FILE BEFORE any
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
        """litclock-dev#331 — `litclock-shutdown.service` is a stop-hook unit
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
                f"'Powered Off' on the e-ink mid-update (litclock-dev#331). "
                f"Offending line: {line.strip()}"
            )

    def test_sources_lib_state(self, update_sh_content):
        """litclock-dev#241 D3 — atomic helpers were factored to scripts/lib/state.sh.
        update.sh must source it instead of redefining them inline."""
        # litclock-dev#782 — EXECUTED lines. The `source` lines are described in
        # neighbouring comments naming lib/state.sh, so the raw form stayed green
        # with all three executed `source` lines deleted (measured). This is the
        # material one: every atomic write in update.sh goes through those helpers.
        assert "lib/state.sh" in _executed_lines(update_sh_content), (
            "update.sh must source scripts/lib/state.sh"
        )
        # And the helpers must NOT be redefined inside update.sh itself.
        # `atomic_write_file()` definition would look like the function header.
        import re

        local_def = re.search(r"^atomic_write_file\s*\(\s*\)\s*\{", update_sh_content, re.MULTILINE)
        assert local_def is None, "atomic_write_file must NOT be defined inline in update.sh — source from lib"

    def test_phase5_migrates_legacy_lkg_service_install(self, update_sh_content):
        """litclock-dev#241 — pre-litclock-dev#241 Pis have litclock-lkg.service enabled with
        WantedBy=litclock.service, which created a stale .wants/ symlink.
        Phase 5 must disable the OLD unit before cp'ing the new one (which
        has no [Install]) so the symlink is cleaned up and the rewritten
        oneshot doesn't fire on every litclock.service start."""
        # Migration must happen inside Phase 5 (so daemon-reload picks it up).
        phase5_idx = update_sh_content.find("Phase 5: Update systemd units")
        next_phase_idx = update_sh_content.find("Phase 6:", phase5_idx)
        assert phase5_idx != -1 and next_phase_idx != -1
        phase5_block = update_sh_content[phase5_idx:next_phase_idx]
        # litclock-dev#782 — EXECUTED lines: the surrounding comment quotes
        # `WantedBy=litclock.service` while explaining the legacy install.
        assert "WantedBy=litclock.service" in _executed_lines(phase5_block), (
            "Phase 5 must detect the legacy WantedBy=litclock.service install"
        )
        assert "systemctl disable litclock-lkg.service" in phase5_block, (
            "Phase 5 must disable the legacy litclock-lkg.service to clean its .wants/ symlink"
        )

    def test_phase5_respects_user_disabled_units(self, update_sh_content):
        """Regression for litclock-dev#209 hardware-found defect: Phase 5 must NOT
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
            "silently re-enabled (litclock-dev#209 D2 fix)"
        )

    def test_venv_creation_uses_system_site_packages(self, update_sh_content):
        """litclock-dev#214 regression: every `python3 -m venv` call MUST include
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
                f"them (litclock-dev#214). Offending line: {call.strip()}"
            )

    def test_pip_install_filters_apt_provisioned(self, update_sh_content):
        """litclock-dev#214 regression: pip install must filter requirements.txt through
        requirements-apt.txt before installing. Otherwise pip tries to
        install the same packages apt already provides, which on a gcc-less
        image means sdist compilation and failure.

        Must stay in sync with pi-gen/stage3/01-setup-app/00-run.sh:34.
        """
        # litclock-dev#782 — EXECUTED lines. The litclock-dev#214 sdist-compile hazard is
        # explained in a comment that names the file, so the raw form passed
        # with the executed read deleted (measured).
        assert "requirements-apt.txt" in _executed_lines(update_sh_content), (
            "update.sh must read apt-provisioned names from requirements-apt.txt (litclock-dev#214)"
        )
        assert "grep -vE" in update_sh_content, (
            "update.sh must filter requirements.txt with a grep -vE regex "
            "(litclock-dev#214)")
        # The filtered requirements file, not the raw one, must reach pip.
        assert 'install --upgrade -r "$REQUIREMENTS_FILTERED"' in update_sh_content, (
            "pip install must target the filtered requirements, not the raw file (litclock-dev#214)"
        )

    def test_pip_install_uses_upgrade(self, update_sh_content):
        """litclock-dev#321: without `--upgrade`, pip silently keeps an already-installed
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
            "silently fail to propagate to existing venvs (litclock-dev#321)"
        )
        assert "--upgrade-strategy eager" not in flags, (
            "requirements-file pip install must NOT use --upgrade-strategy eager — it would "
            "weekly-upgrade Flask's unpinned transitives and Phase 4.5 smoke wouldn't catch the "
            "blast (litclock-dev#321 adversarial review). Use a release-cut-time lockfile instead."
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



class TestBlockedShaOnSmokeRevert:
    """litclock-dev#865 — a smoke-gate revert must be remembered.

    The suppression already existed (bootcheck writes blocked-sha, Phase 1
    skips a matching TARGET_SHA, a successful apply clears it). Only the WRITE
    was missing on the smoke-revert path, so every weekly tick re-fetched the
    same bad release, re-applied it, failed the same probe and reverted —
    confirmed on hardware over two consecutive ticks.

    These lift `_block_reverted_release` out of the script and EXECUTE it. The
    mutation that matters is not "the write disappeared" (a source-text check
    would see that) but "the wrong SHA is written": blocking REVERT_SHA instead
    of TARGET_SHA pins the device off the only build known to work on it, and
    reads identically in the diff.
    """

    BAD = "c5c4a135064f7f482b43d2e6a9c5d42d1bbfe4c5"   # the release that failed smoke
    GOOD = "787d3079163aa94a1936e4b57535e87ec34df8cc"  # what we reverted TO

    def _run(self, tmp_path, update_sh_content, *, target, rollback=0, state_writable=True,
             no_interpreter=0):
        """Execute the lifted helper with controlled globals; return (blocked, log)."""
        import subprocess

        start = update_sh_content.index("_block_reverted_release() {")
        end = update_sh_content.index("\n}\n", start) + len("\n}\n")
        frag = update_sh_content[start:end]

        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        blocked = state / "blocked-sha"
        blocked.unlink(missing_ok=True)  # the malformed-input case calls this in a loop
        if not state_writable:
            state.chmod(0o555)

        script = f"""
        log_info()  {{ echo "INFO $*"; }}
        log_warn()  {{ echo "WARN $*"; }}
        atomic_write_file() {{ printf '%s\\n' "$2" > "$1" 2>/dev/null; }}
        BLOCKED_SHA_FILE="{blocked}"
        ROLLBACK_MODE={rollback}
        smoke_no_interpreter={no_interpreter}
        {frag}
        _block_reverted_release "{target}"
        """
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        if not state_writable:
            state.chmod(0o755)
        return (blocked.read_text().strip() if blocked.exists() else None), r.stdout + r.stderr

    def test_blocks_the_release_that_failed_not_the_one_we_reverted_to(self, tmp_path, update_sh_content):
        """THE mutation that matters. Writing REVERT_SHA here would block the
        last build known to work on this device — a permanent pin, from a
        one-word change that looks right."""
        got, log = self._run(tmp_path, update_sh_content, target=self.BAD)
        assert got == self.BAD, f"blocked the wrong SHA: {got}"
        assert got != self.GOOD
        assert "blocked" in log

    def test_a_gate_that_could_not_RUN_does_not_blame_the_release(self, tmp_path, update_sh_content):
        """`smoke_no_interpreter` means $PYTHON was gone after Phase 4 — a fault
        in THIS DEVICE's venv, not evidence about the release (litclock-dev#773's
        "nothing was verified"). Blocking there pins the device off a release
        that is probably fine, and outlives the fault: the Phase-4 guard
        rebuilds a broken venv next tick, so the device heals and then refuses
        the release it could now install. Same reasoning as the pip-failure
        exclusion, one step later."""
        got, log = self._run(tmp_path, update_sh_content, target=self.BAD, no_interpreter=1)
        assert got is None, "a device-side venv fault must not block the release"
        assert "could not run" in log

    def test_rollback_mode_writes_nothing(self, tmp_path, update_sh_content):
        """In rollback mode TARGET_SHA is the LKG target bootcheck is recovering
        TO. Blocking it would strand the device off its own last-known-good, and
        bootcheck owns the file in that mode."""
        got, log = self._run(tmp_path, update_sh_content, target=self.BAD, rollback=1)
        assert got is None, "must not touch blocked-sha during a bootcheck rollback"
        assert "rollback mode" in log

    def test_a_missing_or_malformed_target_is_refused_loudly(self, tmp_path, update_sh_content):
        """An empty TARGET_SHA must not write a truncated/garbage block that
        never matches anything (a silent no-op) — and must say so, since the
        device will retry the bad release next tick."""
        for bogus in ("", "notasha", self.BAD[:39]):
            got, log = self._run(tmp_path, update_sh_content, target=bogus)
            assert got is None, f"wrote a malformed block for {bogus!r}"
            assert "no usable target SHA" in log

    def test_an_unwritable_state_dir_warns_and_does_not_abort(self, tmp_path, update_sh_content):
        """A revert that cannot record the block is still a completed revert.
        The warning has to name the consequence, because the only symptom
        otherwise is the loop this fix removes."""
        got, log = self._run(tmp_path, update_sh_content, target=self.BAD, state_writable=False)
        assert got is None
        assert "could not write" in log.lower()
        assert "re-install and re-revert" in log

    def test_the_smoke_revert_arm_calls_it_with_the_failing_sha(self, tmp_path, update_sh_content):
        """Wiring, judged by EFFECT rather than spelling.

        Lifts the real call line off the smoke-revert arm and runs it with both
        SHAs in scope. A string compare would pass `${TARGET_SHA}` and fail the
        equivalent `"$TARGET_SHA"`, and — worse — would not distinguish a call
        that passes the wrong variable from one that passes the right one if
        the spelling happened to match. Here the assertion is which SHA lands
        in the file.
        """
        import re
        import subprocess

        code = "\n".join(ln.split("#", 1)[0] for ln in update_sh_content.splitlines())
        smoke = code.index("Smoke test failed (exit $smoke_rc)")
        window = code[smoke:smoke + 3000]
        m = re.search(r"^\s*_block_reverted_release\s+(.+)$", window, re.M)
        assert m, "the smoke-revert arm does not call _block_reverted_release"
        call = m.group(0).strip()

        start = update_sh_content.index("_block_reverted_release() {")
        end = update_sh_content.index("\n}\n", start) + len("\n}\n")
        frag = update_sh_content[start:end]

        blocked = tmp_path / "blocked-sha"
        script = f"""
        log_info() {{ :; }}
        log_warn() {{ :; }}
        atomic_write_file() {{ printf '%s\\n' "$2" > "$1" 2>/dev/null; }}
        BLOCKED_SHA_FILE="{blocked}"
        ROLLBACK_MODE=0
        TARGET_SHA="{self.BAD}"
        REVERT_SHA="{self.GOOD}"
        {frag}
        {call}
        """
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        got = blocked.read_text().strip() if blocked.exists() else None
        assert got == self.BAD, (
            f"the smoke-revert arm blocked {got}; it must block the release that FAILED "
            f"({self.BAD}), not the one it reverted to ({self.GOOD})"
        )

    def test_pip_failure_arm_does_not_block(self, update_sh_content):
        """A failed pip is usually the network. Blocking a good release on a
        transient wheel fetch would be worse than the bug this fixes."""
        code = "\n".join(ln.split("#", 1)[0] for ln in update_sh_content.splitlines())
        pip = code.index("pip install failed")
        window = code[pip:pip + 2000]
        assert "_block_reverted_release" not in window


class TestBlockedShaSkip:
    """The consume side of blocked-sha, which now has TWO writers.

    litclock-bootcheck's write is covered in test_bootcheck.py; the SKIP it
    depends on was covered nowhere. litclock-dev#865 added a second writer, so
    the contract they both rely on is pinned here: a matching SHA short-circuits
    the run, anything else falls through to a normal install.

    Executed, not grepped — a fall-through is exactly what source text cannot
    see, and a fall-through here means the device re-installs the release it
    just reverted from, which is the loop litclock-dev#865 removed.
    """

    BLOCKED = "6ab1f3c7c9105f9201a2a73230f203efdf01ba09"
    OTHER = "316a835a520a5acec9f929b60a43e3061158d1f3"

    def _run(self, tmp_path, update_sh_content, *, target, file_contents):
        import subprocess

        start = update_sh_content.index('if [[ -n "$TARGET_SHA" && -f "$BLOCKED_SHA_FILE" ]]; then')
        # End on the 4-space `fi` that closes THIS `if`, not the column-0 one
        # that closes the enclosing rollback/resolver chain — taking the latter
        # lifts an extra `fi` and the fragment dies on a syntax error, which
        # looks exactly like a fall-through and would have made every
        # falls-through assertion below pass for the wrong reason.
        end = update_sh_content.index("\n    fi\n", update_sh_content.index("exit 0", start)) + len("\n    fi\n")
        frag = update_sh_content[start:end]

        blocked = tmp_path / "blocked-sha"
        if file_contents is not None:
            blocked.write_text(file_contents)

        script = f"""
        log_info() {{ echo "[INFO] $*"; }}
        log_warn() {{ echo "[WARN] $*"; }}
        sudo() {{ :; }}
        update_status_complete() {{ echo "STATUS_COMPLETE"; }}
        read_sha_file() {{
            [ -s "$1" ] || {{ printf ''; return 0; }}
            v=$(tr -cd '0-9a-f' < "$1")
            case "$v" in [0-9a-f]*) [ ${{#v}} -eq 40 ] && printf '%s' "$v" || printf '';; *) printf '';; esac
        }}
        BLOCKED_SHA_FILE="{blocked}"
        TARGET_SHA="{target}"
        _LITCLOCK_UPDATE_FINALIZED=0
        {frag}
        echo "REACHED_INSTALL"
        """
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return r.stdout

    def test_a_matching_sha_short_circuits_the_run(self, tmp_path, update_sh_content):
        out = self._run(tmp_path, update_sh_content, target=self.BLOCKED, file_contents=self.BLOCKED)
        assert "REACHED_INSTALL" not in out, "a blocked release must not be installed again"
        assert "is blocked" in out
        assert "STATUS_COMPLETE" in out, "a deliberate no-op is 'complete', not a failure"

    def test_a_different_sha_falls_through_so_recovery_is_possible(self, tmp_path, update_sh_content):
        """The half that keeps a blocked device recoverable. If this regressed,
        the block would be permanent and a newer release could never land."""
        out = self._run(tmp_path, update_sh_content, target=self.OTHER, file_contents=self.BLOCKED)
        assert "REACHED_INSTALL" in out
        assert "is blocked" not in out

    def test_an_empty_or_garbage_block_file_falls_through(self, tmp_path, update_sh_content):
        """A truncated write must not wedge updates: read_sha_file returns empty
        for anything that is not a 40-char hex SHA, and empty must not match."""
        for contents in ("", "   \n", "not-a-sha", self.BLOCKED[:39]):
            out = self._run(tmp_path, update_sh_content, target=self.BLOCKED, file_contents=contents)
            assert "REACHED_INSTALL" in out, f"a {contents!r} block file wedged the updater"

    def test_no_block_file_at_all_falls_through(self, tmp_path, update_sh_content):
        out = self._run(tmp_path, update_sh_content, target=self.BLOCKED, file_contents=None)
        assert "REACHED_INSTALL" in out

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
        """Regression for litclock-dev#209 hardware-found defect: after firstboot's
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
            "/etc/litclock/.setup-complete being present (litclock-dev#209 regression)"
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


# ── litclock-dev#209 structural tests ─────────────────────────────────────────────
# The weekly auto-update cycle adds state (LKG marker, update-failed marker)
# and a new revert path (Phase 4.5 smoke failure). Existing execution-sandbox
# tests can't cover these without a real Python venv + network, so these are
# grep-based invariants against the script source.


class TestAutoUpdateStructure:
    def test_phase1_does_not_clear_lkg_marker(self, update_sh_content):
        """LKG auto-revert (litclock-dev#209 follow-up) INVERTED the old invariant: Phase 1
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
        update, and pins the fleet (litclock-dev#209 hardware-found regression)."""
        assert 'timeout 60 "$PYTHON" src/literary_clock.py --dry-run' in update_sh_content, (
            "smoke test must invoke literary_clock.py script-style (matching "
            "runtheclock.sh), not via `python -m src.literary_clock`"
        )
        # Defense in depth: the broken module-style invocation must NOT
        # creep back in.
        assert "-m src.literary_clock" not in update_sh_content, (
            "smoke test must NOT use module-style invocation — breaks the "
            "absolute `from log import` resolution. See litclock-dev#209 hardware QA."
        )

    def test_smoke_failure_reverts_head_and_wipes_hash(self, update_sh_content):
        """On smoke-test failure, revert to REVERT_SHA AND delete the pip hash
        file so the next run re-runs `pip install`. (Deleting the hash sets
        NEED_PIP; it does NOT by itself recreate the venv — the falsified
        "rebuilds from scratch" phrasing this docstring used to carry is the
        one `test_pip_install_failure_comment_honest_about_hash_delete`
        below forbids in the script.) Leaving the hash in place would mean the
        partially-updated venv appears clean to Phase 4's hash gate on the next
        timer fire.

        REVERT_SHA == OLD_SHA in a normal update; in bootcheck rollback mode it
        is the LKG target, so a failure stays on the last-known-good rather than
        the bad code (litclock-dev#209 LKG auto-revert)."""
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
        """litclock-dev#324 + codex adversarial review of PR litclock-dev#349: when pip install
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
            "pip-install failure must log a revert, not just a will-retry message (litclock-dev#324)"
        )

        # The branch must terminate with exit 1 BEFORE Phase 4.5 smoke runs.
        exit_idx = update_sh_content.find("exit 1", fail_idx)
        phase4_5_idx = update_sh_content.find("Phase 4.5: Post-rebuild smoke test")
        assert exit_idx != -1, "pip-install failure branch must exit non-zero"
        assert phase4_5_idx != -1, "Phase 4.5 heading missing"
        assert exit_idx < phase4_5_idx, (
            "pip-install failure must exit BEFORE Phase 4.5 smoke runs (litclock-dev#324) — otherwise the "
            "partial venv proceeds against smoke, which can pass on dep gaps it never imports"
        )

        fail_block = update_sh_content[fail_idx:exit_idx]
        assert 'git reset --hard "$REVERT_SHA"' in fail_block, (
            "pip-install failure branch must revert git to REVERT_SHA "
            "(== OLD_SHA normally; the LKG target in bootcheck rollback mode) (litclock-dev#324)"
        )
        assert 'rm -f "$HASH_FILE"' in fail_block, (
            "pip-install failure branch must delete the pip hash so the next run re-attempts pip install "
            "(litclock-dev#324)"
        )
        assert "$UPDATE_FAILED_FILE" in fail_block, (
            "pip-install failure branch must write the update-failed marker for the e-ink glyph (litclock-dev#324)"
        )

        # Codex Finding 1 (HIGH): terminal status MUST be failed_unrecovered,
        # not failed_reverted — venv state is indeterminate after a
        # mid-stream pip failure, and reverting git does not restore packages.
        assert "update_status_failed_unrecovered" in fail_block, (
            "pip-install failure must record terminal status as failed_unrecovered — venv state "
            "is indeterminate after mid-stream pip failure, so failed_reverted would lie about "
            "the clock being back on a known-good state (codex Finding 1 / litclock-dev#324)"
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
        # litclock-dev#782 — EXECUTED lines throughout this test. update.sh's own
        # comments quote `_LITCLOCK_UPDATE_FINALIZED=1` and `REVERT_OK` while
        # explaining them, so the raw forms were satisfied by prose: measured
        # green with all 4 executed FINALIZED lines and all 8 executed REVERT_OK
        # lines deleted. Both find() offsets must come from the SAME executed
        # string or the ordering comparison is meaningless.
        fail_executed = _executed_lines(fail_block)
        assert "_LITCLOCK_UPDATE_FINALIZED=1" in fail_executed, (
            "pip-install failure must set _LITCLOCK_UPDATE_FINALIZED=1 (litclock-dev#324)"
        )
        finalized_idx = fail_executed.find("_LITCLOCK_UPDATE_FINALIZED=1")
        status_write_idx = fail_executed.find("update_status_failed_unrecovered")
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
        assert "REVERT_OK" in fail_executed, (
            "pip-install failure branch must capture revert exit codes via REVERT_OK so we can "
            "tell 'code reverted, venv uncertain' from 'revert itself failed' (codex Finding 2)"
        )

        # The "|| true" patterns from the original commit must be gone —
        # they swallowed real failure signal. The branch should rely on
        # explicit if-tests + PIPESTATUS instead.
        assert "git reset --hard \"$OLD_SHA\" 2>&1 | sed 's/^/[revert] /' || true" not in fail_executed, (
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

        # litclock-dev#782 — both offsets from the same EXECUTED string.
        fail_executed = _executed_lines(fail_block)
        finalized_idx = fail_executed.find("_LITCLOCK_UPDATE_FINALIZED=1")
        unrecovered_idx = fail_executed.find("update_status_failed_unrecovered")
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

        # The "from scratch" wording overstates what `rm -f HASH_FILE` does —
        # it sets NEED_PIP, it does not recreate the venv.
        #
        # This comment used to add that the smoke-fail branch "still uses
        # similar wording but ... semantics differ". That justification was
        # rejected in review (litclock-dev#773) and the wording is gone from
        # that branch too, so the carve-out is stale. The assert stays scoped to
        # the pip-fail block because that is the block this test lifts; the
        # whole-file version is the self-heal comment audit on the same issue.
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

        Post-litclock-dev#241 the helper lives in scripts/lib/state.sh — verify the
        definition there and that update.sh sources the lib."""
        import re
        from pathlib import Path

        # litclock-dev#782 — EXECUTED lines (see test_sources_lib_state).
        assert "lib/state.sh" in _executed_lines(update_sh_content), (
            "update.sh must source the shared atomic helpers"
        )
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
        tolerate a missing lib (fresh-image runs before litclock-dev#209 lands)."""
        assert "lib/github_api.sh" in update_sh_content
        # Guard: the source must be conditional, not unconditional.
        import re

        assert re.search(
            r'\[\[ -f "\$_THIS_SCRIPT_DIR/lib/github_api\.sh" \]\]',
            update_sh_content,
        ), "source must be wrapped in a -f guard so legacy path still works"


class TestPersistentLastUpdateWriter:
    """litclock-dev#334 — update.sh writes a persistent /var/lib/litclock/last-update.json
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
        """litclock-dev#334 Tension 3 — flock-based single-flight guard. Two concurrent
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
        """litclock-dev#334 Tension 1 — `_LITCLOCK_UPDATE_FINALIZED=1` must be set
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
        """litclock-dev#334 Tension 2 — before staging the persistent file, update.sh
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
        # litclock-dev#782 — EXECUTED lines: this block's docstring and comments
        # both say "jq -e".
        assert "jq -e" in _executed_lines(block), (
            "validate-then-cp must use jq -e for the boolean exit code"
        )
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
        """litclock-dev#342 I2 — the validate-then-cp gate's freshness floor must be
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
            "freshness floor must be 3600s (widened from 60s in litclock-dev#342 I2) — narrower "
            "windows lose the persist write on cold-boot updates that race chrony"
        )
        # And the prior tight 60s window must NOT linger.
        assert "now_unix - 60)" not in block, (
            "old 60s freshness floor must be gone — leftover 60s would defeat the I2 widening"
        )

    def test_persist_sweeps_orphan_tmp_files(self, update_sh_content):
        """litclock-dev#342 I4 — orphan sweep before staging the new .tmp.<pid> file.
        Mirrors the manifest-sweep pattern from PR litclock-dev#293: if a prior persist
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
    """litclock-dev#274 follow-up #5 — Phase 3 flock-timeout marker.

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
    """EPIC litclock-dev#383 PR2 (litclock-dev#388) Option-A migration: the upgraded litclock.service
    is gated on .handoff-complete, which pre-PR2 devices never wrote. update.sh
    must touch it so quotes don't stop on upgrade — a Pi glued in its case would
    go dark.

    CONDITIONALLY since litclock-dev#675: "we confirmed .setup-complete, so this
    device is past first-boot" is false during the handoff window. The migration
    now fires when the timezone is known OR the installed unit predates the
    gate; see TestHandoffMigrationRespectsTheTimezone for the behaviour. This
    stays as a structural check that the write still exists at all."""
    src = UPDATE_SH.read_text()
    assert "/etc/litclock/.handoff-complete" in src
    assert "sudo touch /etc/litclock/.handoff-complete" in src


class TestRuntimeMarkerInvalidation:
    """litclock-dev#604 — the validation marker is gitignored, so the
    Phase-2 git reset never touches it; update.sh itself must remove it
    when any proof input changed in the release being applied."""

    def test_marker_removal_block_exists_with_every_proof_input(self):
        src = UPDATE_SH.read_text()
        assert ".runtime-render-validated" in src
        block_start = src.index("RUNTIME_MARKER=")
        block = src[block_start : src.index("Remove old systemd units", block_start)]
        # The delete is gated on the diff of the proof inputs, not unconditional.
        assert "git diff --quiet" in block
        for proof_input in (
            "fonts/",
            "tools/gd-expected-measurements.json.gz",
            "requirements.txt",
            "src/quote_renderer.py",
            "src/gd_measure.py",
            "tools/validate_measurement.py",
        ):
            assert proof_input in block, f"{proof_input} missing from the invalidation trigger set"
        assert 'rm -f "$RUNTIME_MARKER"' in block

    def test_marker_removal_runs_after_the_git_reset(self):
        """The diff needs the NEW tree (and NEW_SHA) to exist — before the
        reset it would compare the wrong states and the marker would
        survive the exact updates that invalidate it."""
        src = UPDATE_SH.read_text()
        assert src.index('NEW_SHA=$(git rev-parse --short HEAD)') < src.index("RUNTIME_MARKER=")

    def test_marker_removal_diffs_old_to_new(self):
        src = UPDATE_SH.read_text()
        block = src[src.index("RUNTIME_MARKER=") :]
        assert '"$OLD_SHA" "$NEW_SHA"' in block.split("Remove old systemd units")[0]

    def test_marker_path_honors_the_env_override(self):
        """litclock-dev#611 review — the reader honors
        LITCLOCK_RUNTIME_VALIDATED_MARKER (via env.sh); update.sh must
        resolve the same path or a relocated marker silently survives the
        only invalidation layer covering semantics-only proof changes."""
        src = UPDATE_SH.read_text()
        start = src.index("RUNTIME_MARKER=")
        block = src[start : src.index("Remove old systemd units", start)]
        assert "LITCLOCK_RUNTIME_VALIDATED_MARKER" in block
        assert 'source "$INSTALL_DIR/env.sh"' in block

    def test_git_diff_failure_also_removes_the_marker_with_an_honest_log(self):
        """A gc'd SHA or corrupt object makes git diff exit >1 under
        2>/dev/null. We cannot PROVE the inputs unchanged, so the marker
        still goes (wrong fallback beats wrong render) — but under its own
        log line, not the 'inputs changed' claim."""
        src = UPDATE_SH.read_text()
        start = src.index("RUNTIME_MARKER=")
        block = src[start : src.index("Remove old systemd units", start)]
        assert "_marker_diff_rc" in block
        assert "-gt 1" in block
        assert block.count('rm -f "$RUNTIME_MARKER"') == 2
        assert "could not verify" in block


# --- litclock-dev#682 ------------------------------------------------------

# Every `chmod` in update.sh must be accounted for here. Anything this file
# cannot classify fails loudly rather than going unchecked: the guard below is
# only as good as its inventory, and an ADDED chmod line is the likely edit.
# (An added `chmod +x "${INSTALL_DIR}"/scripts/lib/*.sh` in brace form left an
# earlier version of these tests green while dirtying three tracked files.)
CHMOD_NEEDS_NO_TRACKED_MODE = {
    # target                      why it needs no tracked-mode guard
    "$ROLLBACK_SELF_SNAPSHOT": "a /tmp snapshot of this script, not a repo path",
    "$tmp": "a status tempfile, not a repo path (and 0644, not +x)",
    # litclock-dev#847 item 1 (litclock-dev#854 review): the runtime-render validation memo
    # under /var/lib/litclock. Not a repo path, and the chmod is 0644 — it
    # undoes mktemp's 0600 so the pi-user control_server can read a memo a
    # root-run update wrote.
    "$RUNTIME_VALIDATION_MEMO_FILE": "a /var/lib state file, not a repo path (and 0644, not +x)",
}

# Targets that are a single repo file rather than a glob.
CHMOD_SINGLE_FILE_TARGETS = {
    "$SELF_SCRIPT": "scripts/update.sh",  # readlink -f "${BASH_SOURCE[0]}"
}


def _chmod_lines():
    """Every executable `chmod` line in update.sh, comments excluded."""
    return [
        line.strip()
        for line in UPDATE_SH.read_text().splitlines()
        if "chmod" in line and not line.lstrip().startswith("#")
    ]


def _classify_chmod(line):
    """('glob', pattern) | ('file', repo_path) | ('exempt', reason) | None.

    None means "unrecognised", which the inventory test turns into a failure.
    Deliberately tolerant about the shapes it accepts (braces, `--`, trailing
    comments, multiple targets) so that a real edit is either checked or
    reported, never silently dropped.
    """
    m = re.match(r"^chmod\s+(?:--\s+)?([0-7]{3,4}|[augo]*\+x)\s+(.*?)(?:\s*(?:#|2>|\|\||&&).*)?$", line)
    if not m:
        return None
    mode, targets = m.group(1), m.group(2)
    if not mode.endswith("+x"):
        # Not an executable-bit change; only the tracked +x modes matter here.
        for exempt in CHMOD_NEEDS_NO_TRACKED_MODE:
            if exempt in targets:
                return ("exempt", CHMOD_NEEDS_NO_TRACKED_MODE[exempt])
        return None

    for exempt, reason in CHMOD_NEEDS_NO_TRACKED_MODE.items():
        if exempt in targets:
            return ("exempt", reason)
    for var, repo_path in CHMOD_SINGLE_FILE_TARGETS.items():
        if var in targets:
            return ("file", repo_path)

    m = re.search(r'"?\$\{?INSTALL_DIR\}?"?/"?([^"\s]+)"?', targets)
    if m:
        return ("glob", m.group(1))
    return None


def _tracked_modes():
    """path -> mode, straight from the index.

    `-z` matters: without it `git ls-files -s` C-quotes any path with non-ASCII
    or special bytes, so `scripts/wünicode.sh` came back as the 25-character
    escaped string, never matched, and fell through the untracked branch — the
    guard went green on exactly the violation it exists to catch.
    """
    out = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=60
    ).stdout
    modes = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        modes[path] = meta.split()[0]
    return modes


def _bash_glob_match(pattern, path):
    """Does bash's glob `pattern` match `path`, with the options update.sh runs?

    NOT pathlib's `Path.glob`, which differs from bash in two directions that
    both matter here: it matches dotfiles (bash needs `dotglob`, which update.sh
    never sets, so a committed `scripts/.hidden.sh` produced a FALSE failure
    demanding +x on a file the device never chmods), and it treats `**` as
    recursive (bash needs `globstar`, also never set, so `scripts/**/*.sh`
    expands to `scripts/lib/*.sh` ALONE — it replaces the top level rather than
    adding to it).
    """
    pattern_segments = pattern.split("/")
    path_segments = path.split("/")
    if len(pattern_segments) != len(path_segments):
        return False
    for pattern_segment, path_segment in zip(pattern_segments, path_segments, strict=True):
        if path_segment.startswith(".") and not pattern_segment.startswith("."):
            return False
        if not fnmatch.fnmatchcase(path_segment, pattern_segment):
            return False
    return True


class TestChmodTargetsAreTrackedExecutable:
    """Every file update.sh chmods must already be tracked executable.

    Otherwise the chmod is a real mode change on every device on every update:
    Phase 2's `git reset --hard` overwrites the mode-only dirt exactly as the
    warning promises, and then Phase 6 of the SAME run re-creates it, so the
    tree is dirty again before the run ends. The next update then warns the
    operator about uncommitted changes the previous update caused.

    Nothing breaks. The cost is that ``[WARN] Uncommitted changes detected`` is
    exactly the line that should make an operator stop and look if they had
    really hand-edited something on the device, and instead it fires on every
    update and is wrong every time (litclock-dev#682).

    Observed on the fleet sentinel against v0.224.0, where the tracked mode of
    ``scripts/qa-reresolve-hw-test.sh`` was 100644. In the development repo it
    was always 100755; the mode was lost in the port to THIS repo (it arrived
    100644 in #51) and was repaired as the symptom in #59 — with nothing to
    stop a recurrence until this guard, which is derived from the chmod lines
    in update.sh rather than pinned to that one file.

    ``scripts/lib/*.sh`` are deliberately not covered: they are sourced, never
    run, none has a shebang, and `scripts/*.sh` does not recurse into them. If
    the glob is ever widened to reach them, the inventory test below fails until
    someone decides what their modes should be.

    Related but separate: ``tests/test_pi_gen.py::test_chroot_scripts_are_executable``
    covers pi-gen's chroot scripts. It asserts the FILESYSTEM mode, which passes
    on any working tree where the bit happens to be set locally even when git
    records 100644 — precisely the state this class exists to catch. The two file
    sets are disjoint, so this is a note, not a duplicate.
    """

    def test_every_chmod_line_is_accounted_for(self):
        """Fail closed on an unrecognised chmod. The guard is only as good as
        its inventory, and an ADDED line is the likely edit — an earlier version
        of this test stayed green while a brace-form chmod dirtied three tracked
        files."""
        lines = _chmod_lines()
        assert lines, "no chmod at all in update.sh — this test went blind"

        unrecognised = [line for line in lines if _classify_chmod(line) is None]
        assert not unrecognised, (
            "update.sh has chmod lines this guard cannot classify, so it cannot tell whether they "
            "dirty tracked files:\n  "
            + "\n  ".join(unrecognised)
            + "\n\nAdd the target to CHMOD_SINGLE_FILE_TARGETS or CHMOD_NEEDS_NO_TRACKED_MODE, "
            "or widen _classify_chmod."
        )
        assert any(kind == "glob" for kind, _ in map(_classify_chmod, lines)), (
            "update.sh no longer chmods any $INSTALL_DIR glob — the guard below now checks nothing"
        )

    def test_every_chmod_target_is_tracked_executable(self):
        modes = _tracked_modes()
        offenders = []

        for line in _chmod_lines():
            classified = _classify_chmod(line)
            assert classified is not None, line
            kind, value = classified
            if kind == "exempt":
                continue

            if kind == "file":
                mode = modes.get(value)
                assert mode is not None, f"{line!r} chmods {value}, which git does not track"
                if mode != "100755":
                    offenders.append(f"{value} (tracked {mode}) via {line!r}")
                continue

            # kind == "glob": check every TRACKED path it matches. Enumerating
            # the index rather than the filesystem means a sparse or partially
            # checked-out worktree cannot hide a path the device would chmod.
            matched = [path for path in modes if _bash_glob_match(value, path)]
            assert matched, (
                f"{line!r} chmods {value!r}, which matches no tracked file — "
                "either the glob is wrong or this test is checking nothing"
            )
            offenders += [f"{path} (tracked {modes[path]}) via {line!r}" for path in matched if modes[path] != "100755"]

        assert not offenders, (
            "update.sh chmods these files but git records them non-executable, so every update on every "
            "device re-dirties the tree and the next update's 'Uncommitted changes detected' warning "
            "becomes noise:\n  " + "\n  ".join(offenders) + "\n\nFix with: git update-index --chmod=+x <path>"
        )


class TestBashGlobMatch:
    """The matcher the guard above depends on. pathlib's Path.glob was wrong in
    both directions, so the replacement gets its own coverage rather than being
    trusted."""

    @pytest.mark.parametrize(
        "pattern,path,expected",
        [
            ("scripts/*.sh", "scripts/update.sh", True),
            ("scripts/*.sh", "scripts/lib/state.sh", False),  # * does not cross /
            ("scripts/*.sh", "scripts/.hidden.sh", False),  # dotglob is off
            ("scripts/*.sh", "scripts/tool.py", False),
            ("scripts/*.sh", "image-gen/x.sh", False),
            ("scripts/**/*.sh", "scripts/lib/state.sh", True),  # globstar off: ** is one segment
            ("scripts/**/*.sh", "scripts/update.sh", False),  # ...so this is NOT covered any more
            ("scripts/.*.sh", "scripts/.hidden.sh", True),  # explicit leading dot does match
            ("scripts/*.sh", "scripts/wünicode.sh", True),  # non-ASCII is ordinary
        ],
    )
    def test_it_matches_what_bash_would(self, pattern, path, expected):
        assert _bash_glob_match(pattern, path) is expected


# --- litclock-dev#675 ------------------------------------------------------


class TestHandoffMigrationRespectsTheTimezone:
    """update.sh writes .handoff-complete, which is what lets quotes start.

    Design-review A2's rule — a clock that paints at the WRONG time is worse
    than no clock — binds every automatic writer. handoff.py and
    litclock-handoff-fallback.sh both refuse when the timezone is unknown; this
    one reasoned "we confirmed .setup-complete, so this device is past
    first-boot", which is false during the handoff window, the window DEFINED as
    .setup-complete present and .handoff-complete absent (litclock-dev#675).

    The discriminator is the INSTALLED UNIT, not a clock. A "refuse if setup
    finished recently" version was written first and is wrong both ways: it
    expires exactly when it is needed (a marker still missing an hour after
    setup is very nearly a definition of "timezone unknown", and the weekly
    timer's RandomizedDelaySec=7d lands inside a one-hour window ~0.6% of the
    time), and it depends on a clock this RTC-less hardware does not have.

    These run the real script. The `sudo` stub logs its arguments without
    executing, so the assertion is on whether update.sh ATTEMPTED the touch —
    which is the decision under test.
    """

    # "gated" means the installed unit already carries the handoff condition,
    # i.e. a PR2+ device. The marker path is substituted per-test along with the
    # one in the script, or the grep would compare a real path to a fake one and
    # every device would look pre-PR2.
    GATED = "gated"
    UNGATED = "ungated"
    ABSENT = "absent"

    def _run_with(
        self,
        sandbox,
        tmp_path,
        *,
        env_body,
        unit=GATED,
        handoff_done=False,
    ):
        fake_etc = tmp_path / "etc" / "litclock"
        fake_etc.mkdir(parents=True)
        (fake_etc / ".setup-complete").write_text("ok\n")
        if handoff_done:
            (fake_etc / ".handoff-complete").write_text("ok\n")

        fake_unit = tmp_path / "systemd" / "litclock.service"
        fake_unit.parent.mkdir(parents=True)
        if unit == self.GATED:
            fake_unit.write_text(f"[Unit]\nConditionPathExists={fake_etc}/.handoff-complete\n")
        elif unit == self.UNGATED:
            fake_unit.write_text("[Unit]\nDescription=AuthorClock Display Update\n")

        _setup_fake_install(sandbox)
        sandbox.stub("sudo")
        sandbox.stub("systemctl")
        sandbox.stub("git", stdout="abc1234\n")
        sandbox.stub("md5sum", stdout="d41d8cd98f00b204e9800998ecf8427e  /tmp/x\n")

        (sandbox.root / "env.sh").write_text(env_body)

        script = sandbox.root / "scripts" / "update.sh"
        script.write_text(
            UPDATE_SH.read_text()
            .replace("/etc/litclock/", f"{fake_etc}/")
            .replace("/etc/systemd/system/litclock.service", str(fake_unit))
        )
        script.chmod(0o755)

        result = sandbox.run(str(script))
        calls = [json.loads(line) for line in sandbox.call_log.read_text().splitlines() if line.strip()]
        marker = str(fake_etc / ".handoff-complete")
        touched = any(
            call["cmd"] == "sudo" and call["args"][:1] == ["touch"] and marker in call["args"] for call in calls
        )
        return result, touched

    # ---------- the reported bug ----------

    def test_it_refuses_a_pr2_device_whose_timezone_is_unknown(self, script_sandbox, tmp_path):
        """An update landing mid-setup would start the clock on whatever
        timezone the Pi currently believes — on a clone, the GIFTER's."""
        result, touched = self._run_with(
            script_sandbox, tmp_path, env_body="export WEATHER_LATITUDE=\nexport WEATHER_LONGITUDE=\n"
        )

        assert not touched, "completed the handoff with an unknown timezone"
        output = result.stdout + result.stderr
        assert "Not completing the handoff" in output
        assert "WRONG time is worse than no clock" in output
        assert "handoff: completed via" not in output, (
            "claimed a completion that did not happen — an operator greps that one string"
        )

    def test_the_refusal_does_not_expire(self, script_sandbox, tmp_path):
        """The whole point of dropping the age condition. A device stuck with an
        unknown timezone stays stuck for days by design (the fallback timer
        refuses on the same predicate), and the weekly updater must not quietly
        complete the handoff on the next tick."""
        fake_etc_marker = tmp_path / "etc" / "litclock"
        result, touched = self._run_with(
            script_sandbox, tmp_path, env_body="export WEATHER_LATITUDE=\nexport WEATHER_LONGITUDE=\n"
        )
        # Age the marker by 90 days and re-run: same answer.
        marker = fake_etc_marker / ".setup-complete"
        stamp = time.time() - 90 * 24 * 3600
        os.utime(marker, (stamp, stamp))
        assert not touched
        assert "Not completing the handoff" in result.stdout + result.stderr

    # ---------- the completion paths ----------

    def test_it_completes_when_the_timezone_is_known(self, script_sandbox, tmp_path):
        result, touched = self._run_with(
            script_sandbox, tmp_path, env_body="export WEATHER_LATITUDE=30.27\nexport WEATHER_LONGITUDE=-97.74\n"
        )
        assert touched, "refused a handoff whose timezone IS known"
        assert "handoff: completed via the in-place updater" in result.stdout + result.stderr

    def test_it_still_migrates_a_genuinely_pre_handoff_device(self, script_sandbox, tmp_path):
        """Brick prevention, and the migration's actual purpose. A pre-PR2
        device with weather turned off has no coordinates and a perfectly correct
        timezone; refusing it stops the quotes on a device that has been painting
        for months. Its INSTALLED unit has no handoff gate, which is what says so
        — no clock involved."""
        result, touched = self._run_with(
            script_sandbox,
            tmp_path,
            env_body="export WEATHER_LATITUDE=\nexport WEATHER_LONGITUDE=\n",
            unit=self.UNGATED,
        )
        assert touched, "refused the pre-PR2 migration and would have stopped a working clock"
        output = result.stdout + result.stderr
        assert "handoff: completed via the in-place updater" in output
        assert "predates the handoff" in output, "the journal must say WHY it migrated"

    def test_a_missing_unit_is_not_treated_as_pre_handoff(self, script_sandbox, tmp_path):
        """Fail safe. Quotes cannot paint without the unit anyway, so refusing
        costs nothing — and it avoids starting a wrong-time clock the moment
        Phase 5 installs the gated unit later in this same run."""
        _, touched = self._run_with(
            script_sandbox,
            tmp_path,
            env_body="export WEATHER_LATITUDE=\nexport WEATHER_LONGITUDE=\n",
            unit=self.ABSENT,
        )
        assert not touched

    # ---------- the predicate ----------

    def test_a_latitude_without_a_longitude_is_not_a_known_timezone(self, script_sandbox, tmp_path):
        """handoff.py requires both. The bash copies checked latitude alone, so
        this env.sh was 'unknown' to the PWA and 'known' here — two writers of
        one marker disagreeing about the predicate that gates it."""
        _, touched = self._run_with(
            script_sandbox, tmp_path, env_body="export WEATHER_LATITUDE=30.27\nexport WEATHER_LONGITUDE=\n"
        )
        assert not touched

    @pytest.mark.parametrize("lat,lon", [("0", "0"), ("0.0", "0.0"), ("-0.0", "-0.0")])
    def test_the_equator_counts_as_a_known_timezone(self, script_sandbox, tmp_path, lat, lon):
        """0 is a legitimate coordinate. A numeric emptiness check would refuse
        every device on the equator, so the string test is pinned."""
        _, touched = self._run_with(
            script_sandbox, tmp_path, env_body=f"export WEATHER_LATITUDE={lat}\nexport WEATHER_LONGITUDE={lon}\n"
        )
        assert touched

    def test_a_blanked_value_with_a_trailing_comment_is_still_blank(self, script_sandbox, tmp_path):
        """`WEATHER_LATITUDE= # unset` is ordinary shell. Without stripping the
        comment the comment TEXT became the value and read as a populated
        coordinate, so a device with no location completed the handoff."""
        _, touched = self._run_with(
            script_sandbox,
            tmp_path,
            env_body="export WEATHER_LATITUDE= # cleared by the PWA\nexport WEATHER_LONGITUDE= # cleared\n",
        )
        assert not touched

    def test_a_missing_env_file_is_not_a_known_timezone(self, script_sandbox, tmp_path):
        _, touched = self._run_with(script_sandbox, tmp_path, env_body="")
        assert not touched

    # ---------- idempotency ----------

    def test_it_does_nothing_when_the_handoff_is_already_complete(self, script_sandbox, tmp_path):
        """Parameters chosen so the completion branch WOULD fire if the
        existence guard were removed — otherwise this passes for an unrelated
        reason and the guard can be deleted with the suite still green."""
        result, touched = self._run_with(
            script_sandbox,
            tmp_path,
            env_body="export WEATHER_LATITUDE=30.27\nexport WEATHER_LONGITUDE=-97.74\n",
            handoff_done=True,
        )
        assert not touched, "the migration must be idempotent"
        assert "handoff: completed via" not in result.stdout + result.stderr


def test_the_two_bash_handoff_writers_share_one_predicate():
    """update.sh and litclock-handoff-fallback.sh both write .handoff-complete
    and cannot import handoff.py, so the predicate is duplicated. It has already
    drifted once — the fallback checked latitude alone while handoff.py's
    _has_location required both coordinates. Pin that all three agree on WHICH
    keys mean "the timezone is known"."""
    fallback = (REPO_ROOT / "scripts" / "litclock-handoff-fallback.sh").read_text()
    updater = UPDATE_SH.read_text()
    handoff_py = (REPO_ROOT / "src" / "control_server" / "handoff.py").read_text()

    for name, source in (("litclock-handoff-fallback.sh", fallback), ("update.sh", updater)):
        for key in ("WEATHER_LATITUDE", "WEATHER_LONGITUDE"):
            assert f"read_env_value {key}" in source, f"{name} no longer reads {key}"
        assert 'value="${value%%#*}"' in source, f"{name} stopped stripping trailing comments from env.sh values"

    # Reading a value and USING it are different claims. An earlier version of
    # this test asserted only the read, so dropping the longitude from the
    # fallback's actual condition left it green.
    assert '[[ -z "$lat_val" || -z "$lon_val" ]]' in fallback, (
        "litclock-handoff-fallback.sh reads the longitude but no longer decides on it"
    )
    assert '[[ -n "$handoff_lat" && -n "$handoff_lon" ]]' in updater, (
        "update.sh reads the longitude but no longer decides on it"
    )

    for key in ("WEATHER_LATITUDE", "WEATHER_LONGITUDE"):
        assert key in handoff_py, f"handoff.py no longer reads {key} — the bash copies now disagree with it"


# --- litclock-dev#676 ------------------------------------------------------


class TestTheHandoffFallbackTimerIsReArmedOnUpdate:
    """litclock-dev#676 gave litclock-handoff-fallback.timer a cadence. Getting that
    cadence onto a device that is ALREADY in the handoff window needs one more
    thing than a new unit file.

    `daemon-reload` does not re-arm a timer that has already elapsed — it keeps
    NextElapse=infinity — and the unit loop starts only timers this run NEWLY
    enabled, which this one never is (pi-gen enables it). Measured both ways on
    systemd 255: reload alone gave 0 firings in 15s at a 5s cadence; a restart
    resumed it immediately. Without the restart the cadence waits for the next
    boot, and the handoff window is exactly the population it exists for.
    """

    def test_it_re_arms_the_timer_after_daemon_reload(self):
        src = UPDATE_SH.read_text()
        assert "systemctl restart litclock-handoff-fallback.timer" in src, (
            "update.sh no longer re-arms the handoff fallback timer, so a device that takes an OTA "
            "while in the handoff window gets the new cadence only after a reboot"
        )
        restart_at = src.index("systemctl restart litclock-handoff-fallback.timer")
        # The LAST daemon-reload before the restart — update.sh has more than
        # one (the authorclock migration has its own), so indexing the first
        # would compare against the wrong marker.
        reload_at = src.rindex("sudo systemctl daemon-reload", 0, restart_at)
        assert reload_at < restart_at, "the re-arm must follow daemon-reload, or it re-arms the OLD unit"
        between = src[reload_at:restart_at]
        assert "for unit in" not in between, "the re-arm must come after the unit-install loop has finished"

    def test_the_re_arm_is_guarded_on_the_unit_being_enabled(self):
        """Restarting a unit the operator disabled would silently undo their
        opt-out — the same respect-user-state rule the enable loop follows."""
        src = UPDATE_SH.read_text()
        restart_at = src.index("systemctl restart litclock-handoff-fallback.timer")
        block = src[max(0, restart_at - 800) : restart_at]
        assert "is-enabled --quiet litclock-handoff-fallback.timer" in block

    def test_the_re_arm_is_not_generalised_to_every_timer(self):
        """Deliberately narrow: restarting litclock.timer mid-update could drop
        a minute's render, and restarting litclock-update.timer from inside
        litclock-update.service is a job-ordering hazard."""
        src = UPDATE_SH.read_text()
        body = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        for risky in ("systemctl restart litclock.timer", "systemctl restart litclock-update.timer"):
            assert risky not in body, f"update.sh now restarts {risky}, which this change deliberately avoided"


# --- litclock-dev#662 ------------------------------------------------------


class TestTheSuiteCannotHangCI:
    """`timeout = 300` in pyproject.toml only does anything when pytest-timeout
    is installed — an absent plugin makes it an "Unknown config option" WARNING
    and the protection silently does nothing. That is the same shape as every
    other guard this batch repaired, one level up, so both halves are pinned:
    the setting must exist AND the plugin must be declared where CI installs it.
    """

    def test_a_per_test_timeout_is_configured(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"^timeout\s*=\s*(\d+)", pyproject, re.M)
        assert match, (
            "pyproject.toml no longer sets a per-test timeout — a hanging test goes back to burning "
            "CI to its wall-clock limit and reporting a timeout instead of the regression"
        )
        seconds = int(match.group(1))
        assert 60 <= seconds <= 900, f"timeout={seconds} is not a usable ceiling"

    def test_the_plugin_that_enforces_it_is_declared(self):
        """Non-comment lines only. A substring check over the raw file stays
        green when the requirement is commented OUT — and this file already
        carries a comment block naming pytest-timeout, so the literal survives
        its own removal (/review, Codex; mutation-proven)."""
        lines = [
            ln.split("#", 1)[0].strip()
            for ln in (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        ]
        declared = [ln for ln in lines if ln.lower().startswith("pytest-timeout")]
        assert declared, (
            "pytest-timeout is not an active requirement, so `timeout = 300` is an inert "
            "unknown-config warning and the suite can hang again"
        )

    def test_ci_installs_the_dev_requirements(self):
        """The declaration only matters if CI installs the file it lives in —
        and again on a non-comment line, since the workflow's own comments
        mention the filename (/review, Codex)."""
        workflow = (REPO_ROOT / ".github" / "workflows" / "lint.yml").read_text(encoding="utf-8")
        installs = [
            ln
            for ln in workflow.splitlines()
            if "pip install" in ln and "requirements-dev.txt" in ln and not ln.lstrip().startswith("#")
        ]
        assert installs, (
            "no uncommented `pip install ... requirements-dev.txt` in lint.yml, so pytest-timeout "
            "never reaches CI and the timeout ceiling is inert there too"
        )


# ───────────────────── litclock-dev#721: origin-derived resolver pair ───────


def _extract_origin_repo_pair() -> str:
    body = UPDATE_SH.read_text()
    start = body.index("origin_repo_pair() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    span = body[start:end]
    assert "git remote get-url origin" in span, "span lost the origin read"
    assert '"kapoorankush" "litclock"' in span, "span lost the fallback pair"
    return span


class TestOriginRepoPair:
    """litclock-dev#721: the resolver asked the hardcoded PUBLIC repo for its
    latest tag regardless of the device's own origin, permanently pinning
    every dev-image device (and half-applying any update that touched
    update.sh, via the checksum re-exec re-resolving a different target).
    The pair now derives from origin; these EXECUTE the real function with a
    stubbed `git`."""

    def _run(self, url_or_fail):
        if url_or_fail is None:
            git_stub = "git() { return 1; }"
        else:
            git_stub = f'git() {{ printf "%s\\n" {shlex.quote(url_or_fail)}; }}'
        script = f"""{git_stub}
{_extract_origin_repo_pair()}
origin_repo_pair
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_https_dev_origin_resolves_dev(self):
        assert self._run("https://github.com/kapoorankush/litclock-dev.git") == "kapoorankush litclock-dev"

    def test_https_without_dot_git(self):
        assert self._run("https://github.com/kapoorankush/litclock-dev") == "kapoorankush litclock-dev"

    def test_ssh_form(self):
        # DEV-shaped on purpose: a public ssh URL's correct answer coincides
        # with the fallback pair, so dropping the ssh parse arm stayed green
        # against it (mutation-caught during /review prep).
        assert self._run("git@github.com:kapoorankush/litclock-dev.git") == "kapoorankush litclock-dev"

    def test_public_https_resolves_public(self):
        assert self._run("https://github.com/kapoorankush/litclock.git") == "kapoorankush litclock"

    def test_unparseable_falls_back_to_public(self):
        assert self._run("ssh://weird.example/foo") == "kapoorankush litclock"

    def test_missing_remote_falls_back_to_public(self):
        assert self._run(None) == "kapoorankush litclock"

    def test_deep_path_falls_back_not_mangles(self):
        """/review litclock-dev#722 F3: `^(.+)/` instead of `^([^/]+)/` survived every
        other shape — the exactly-one-slash rule needs its own pin."""
        assert self._run("https://github.com/o/r/extra") == "kapoorankush litclock"

    def test_trailing_slash_tolerated(self):
        assert self._run("https://github.com/kapoorankush/litclock-dev/") == "kapoorankush litclock-dev"

    def test_credentialed_https_resolves_its_own_pair(self):
        """litclock-dev#728 (/review litclock-dev#729): the origin shape a hand-provisioned
        PAT produces must resolve to ITS pair — the fallback would silently
        reproduce the litclock-dev#721 pinning. DEV-shaped so the fallback can't fake it."""
        assert (
            self._run("https://oauth2:ghp_tok@github.com/kapoorankush/litclock-dev.git")
            == "kapoorankush litclock-dev"
        )

    def test_charset_invalid_component_falls_back(self):
        """Slug charset gate (/review litclock-dev#729): a space would otherwise reach the
        API URL templates."""
        assert self._run("https://github.com/o wner/repo") == "kapoorankush litclock"

    def test_dot_dot_component_falls_back(self):
        assert self._run("https://github.com/../notifications") == "kapoorankush litclock"

    def test_resolver_passes_the_derived_pair_at_runtime(self, tmp_path):
        """EXECUTED wiring pin (/review litclock-dev#722 F6): the textual pin below can be
        satisfied while an overwrite after the read still queries public.
        Lift resolve_target_sha, stub the api function to record its args,
        stub git for both the remote read and the (never-reached) fetch."""
        body = UPDATE_SH.read_text()
        start = body.index("resolve_target_sha() {")
        end = body.index("\n}\n", start) + len("\n}\n")
        fn = body[start:end]
        rec = tmp_path / "args"
        script = f"""git() {{
    case "$1" in
        remote) printf "https://github.com/kapoorankush/litclock-dev.git\\n" ;;
        *) return 1 ;;
    esac
}}
github_api_latest_release_tag() {{ printf "%s %s\\n" "$1" "$2" > {shlex.quote(str(rec))}; }}
{_extract_origin_repo_pair()}
{fn}
resolve_target_sha
"""
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert rec.exists(), "the resolver never queried the api function"
        assert rec.read_text().strip() == "kapoorankush litclock-dev", (
            "a dev-shaped origin produced a non-dev API query at RUNTIME — the litclock-dev#721 defect"
        )

    def test_resolver_actually_uses_the_derived_pair(self):
        """The wiring, not just the parser: resolve_target_sha must pass the
        derived pair to github_api_latest_release_tag. A dev-shaped origin
        must never produce a public-repo query (the exact litclock-dev#721 defect)."""
        body = UPDATE_SH.read_text()
        start = body.index("resolve_target_sha() {")
        end = body.index("\n}\n", start)
        span = "\n".join(
            ln for ln in body[start:end].splitlines() if not ln.lstrip().startswith("#")
        )
        assert 'github_api_latest_release_tag "$_owner" "$_repo"' in span, (
            "resolve_target_sha no longer queries the origin-derived pair (litclock-dev#721)"
        )
        assert 'github_api_latest_release_tag "kapoorankush"' not in span, (
            "a hardcoded repo pair is back in resolve_target_sha — dev-image devices pin forever"
        )
        assert "origin_repo_pair" in span, "the derivation call vanished from resolve_target_sha"


def test_env_sh_lock_is_gitignored():
    """litclock-dev#721 bench pass: env.sh.lock is created by every boot;
    untracked it made `git status --porcelain` permanently non-empty, so
    update.sh's 'Uncommitted changes detected' warning fired on every OTA
    forever — the litclock-dev#682 noise class by another door. Executed against git,
    not grepped against .gitignore prose."""
    r = subprocess.run(
        ["git", "check-ignore", "env.sh.lock"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, "env.sh.lock is not gitignored — every OTA warns about it forever"


# ───────────────────── litclock-dev#724: detached-HEAD warning ──────────────


class TestDetachedHeadWarning:
    """litclock-dev#724 + /review litclock-dev#730: devices are pinned to release-tag
    SHAs, so detached-at-a-release-tag is the fleet's permanent, correct
    state and must be silent — while a detached NON-release commit (hand
    checkout, interrupted-update recovery) and a named non-master branch
    both still warn. These EXECUTE the lifted span with git stubbed
    per-subcommand (a catch-all stub would satisfy any git call and verify
    nothing about the command contract)."""

    def _run(self, branch: str, at_release_tag: bool = True) -> str:
        body = UPDATE_SH.read_text()
        anchor = "CURRENT_BRANCH=$(git rev-parse"
        assert body.count(anchor) == 1, "span anchor must be unique"
        start = body.index(anchor)
        end = body.index("\nfi\n", start) + len("\nfi\n")
        span = body[start:end]
        assert "log_warn" in span, "span lost the warning call"
        assert "git status" not in span, "span overgrew into the porcelain block"
        describe = "printf 'v9.9.9\\n'" if at_release_tag else "return 1"
        script = (
            'git() {\n'
            '  case "$1 $2" in\n'
            '    "rev-parse --abbrev-ref") printf %s\\\\n ' + shlex.quote(branch) + " ;;\n"
            '    "describe --tags") ' + describe + " ;;\n"
            '    "rev-parse --short") printf abc1234\\\\n ;;\n'
            "    *) return 1 ;;\n"
            "  esac\n"
            "}\n"
            'log_warn() { printf \'WARN: %s\\n\' "$1"; }\n' + span
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_detached_at_release_tag_is_silent(self):
        # `git rev-parse --abbrev-ref HEAD` prints literally "HEAD" when
        # detached; a release tag names the commit — the every-run false
        # alarm this issue is about.
        assert "WARN" not in self._run("HEAD", at_release_tag=True)

    def test_detached_at_non_release_commit_warns(self):
        # Hand checkout / interrupted-update recovery: real anomalous state
        # (/review litclock-dev#730 — an absolute suppression would hide it).
        out = self._run("HEAD", at_release_tag=False)
        assert "WARN" in out
        assert "abc1234" in out
        assert "non-release" in out

    def test_master_is_silent(self):
        assert "WARN" not in self._run("master")

    def test_named_branch_still_warns(self):
        out = self._run("feat/experiment")
        assert "WARN" in out
        assert "feat/experiment" in out

    def test_warning_names_no_destination(self):
        # The old text claimed "reset to master" — wrong on the modern path
        # (release target) AND the rollback path (recovery SHA). The new
        # copy must not name any destination (/review litclock-dev#730).
        out = self._run("feat/experiment")
        assert "reset to master" not in out
        assert "master" not in out


# The exact initialiser block Phase 4.5 relies on (litclock-dev#773). Both
# harnesses below LIFT this from the script rather than define their own, so
# deleting or renaming either variable fails the lift instead of being
# silently supplied by the test.
_SMOKE_INIT = "smoke_rc=0\nsmoke_no_interpreter=0\n"


def _executed_lines(text: str) -> str:
    """`text` with comment-only lines dropped.

    A source-text assertion SHOULD run against this rather than the raw span:
    update.sh's comments quote the very tokens the guards anchor on, so a raw
    assert is satisfied by prose while the executed line it names is gone.
    Measured on three separate guards in this file.

    "Should", not "must" — nothing enforces it, and ~115 assertions in this file
    still compare against raw `update.sh` text against only four that come
    through here. Of the 24 whose anchor also occurs in a comment, eight to nine
    were measured comment-satisfiable AND whole-file green (the count moves by
    one with deletion granularity): `REVERT_OK` and `lib/state.sh` are the
    material ones, at eight and three executed lines respectively. All predate
    this helper and are tracked on litclock-dev#773 rather than fixed here
    (/review).
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _full_gate_span(content: str) -> str:
    """The initialisers, the `-x` condition AND the dispatch, in one span.

    The litclock-dev#662 rule: the branch under test has to be INSIDE the
    lifted text.

    The dry-run line is INSIDE this span and IS reached — by the
    `no_interpreter=False` control, whose `$PYTHON` is a stub that exits 1
    immediately, so the real painter never runs. (Two earlier versions of this
    docstring were wrong in opposite directions: "excluded deliberately" — it
    is not excluded — and then "never EXECUTED" — the control reaches it. The
    safety comes from WHAT `$PYTHON` points at, not from the branch being
    unreachable.) Any change that lets a real interpreter reach this line runs
    the painter inside a fixture checkout with no corpus, so re-check the stub
    first.
    """
    start = content.index(_SMOKE_INIT)
    end = content.index('update_status_failed_reverted "Smoke test failed', start)
    end = content.index("fi\n", content.index("exit 1", end)) + len("fi\n")
    span = content[start:end]
    assert span.startswith(_SMOKE_INIT), "the initialisers must lead the span, not be injected"
    # Comment-STRIPPED. Both anchors also occur in COMMENTS inside this span,
    # so the raw-text version was satisfied by prose (/review).
    executed = _executed_lines(span)
    assert "git reset --hard" in executed, "the revert dispatch is missing from the span"
    # And ANCHORED ON THE ELSE ARM, not on the bare token. There are three
    # executed `smoke_rc=1` lines in this span — two probe-failure arms and the
    # missing-interpreter one — so a bare `"smoke_rc=1" in executed` stayed
    # green with the whole else arm gutted to `:`, which is exactly the
    # regression the message claims to catch (/review, round 3). The pair below
    # is unique to the else arm, and the count pins the other two.
    assert "smoke_rc=1\n    smoke_no_interpreter=1" in executed, (
        "the missing-interpreter else arm is missing from the span, or its two assignments are "
        "no longer ADJACENT AND IN THIS ORDER (smoke_rc first). The order is not semantic — "
        "swapping them is behaviour-identical — but the pair pins both flags at once, which a "
        "single-token anchor cannot. If you swapped them deliberately, update this "
        "anchor; the arm itself is driven by the executed tests below"
    )
    # `== 3`, not `>= 1`: a bare `"smoke_rc=1" in executed` was satisfied by any
    # of the three, so gutting the else arm entirely stayed green (/review). The
    # they are the two probe-failure arms, the count-probe arm (litclock-dev#773
    # item 2) and the missing-interpreter arm.
    # (`_gate_span`'s `len(invocations) == 2`, ~80 lines BELOW, is also exact but
    # for a different reason: there the stitch discards a region, so a third
    # probe could land where no test looks. Both are exact; do not read one as
    # the precedent for the other.)
    # Adding a fourth failure arm is legitimate — bump this number and add its
    # own executed test; do not relax the comparison.
    # 4 since litclock-dev#773 item 2 added the catalog-COUNT failure arm; its
    # executed test is TestCatalogTruncationIsCaught below, as this comment
    # requires.
    assert executed.count("smoke_rc=1") == 4, (
        f"expected 4 executed `smoke_rc=1` lines in the span (two probe-failure arms + the "
        f"count-probe arm + the missing-interpreter arm); found {executed.count('smoke_rc=1')}"
    )
    return span


class TestCatalogSmokeGateIsLanguageAgnostic:
    """litclock-dev#763 — the catalog smoke gate compares against hardcoded
    English literals, but `catalog-get` resolves through
    `strings_catalog.active_language()`, which reads the process environment and
    only then env.sh. `litclock-update.service` passes no `LITCLOCK_LANGUAGE`,
    so the probe returned the DEVICE OWNER'S chosen language while the expected
    values stayed English. The full mechanism lives on the code, in the
    litclock-dev#763 comment in `scripts/update.sh`.

    What this class proves, and why it is shaped this way:

    It drives the REAL lifted gate against a REAL second active language,
    through the real `catalog-get` CLI and the real resolver. A stub `$PYTHON`
    would have tested the shape of the gate against a MODEL of the resolver —
    and the resolver's precedence rules are where the entire defect lives, so
    the model is the one part that must not be assumed.

    The span is lifted through the KEEP/revert dispatch, not just the probes.
    Stopping at the probes let `if [[ "$smoke_rc" -eq 0 ]]` -> `if true` on the
    dispatch, and `break` -> `exit 1` inside the loop, both survive: the tests
    saw the error text and no success marker, while production skipped the
    entire revert arm.

    Every probe invocation is logged by a `$PYTHON` wrapper, so a test can
    assert WHICH probes ran and in what order. Output alone cannot: the
    "Splash-triplet smoke passed" line is suppressed by a trailing `&&` whether
    or not the block ran, so asserting on its absence proves nothing about the
    `smoke_rc` gating it appears to prove.
    """

    STATUS_KEY = "status.relative.just_now"
    SPLASH_KEYS = ("boot.splash.starting.title", "firstboot.splash.setup_incomplete.title")

    @staticmethod
    def _gate_span(content: str) -> str:
        """Lift the probe blocks AND the dispatch as ONE span, conditions included.

        litclock-dev#662's rule: a branch condition has to be inside the lifted
        span or the test proves nothing. That applies to the KEEP/revert
        dispatch too — it is the branch the probes exist to drive.
        """
        probe_at = content.index("catalog_probe=")
        start = content.rindex('if [[ "$smoke_rc" -eq 0 ]]; then', 0, probe_at)

        # LIFT the initialisers rather than inject them (/review). Injecting
        # `smoke_rc=0` made deleting it from the script an equivalent mutant —
        # the harness supplied its own. Lifted, a deleted initialiser is an
        # IndexError here, i.e. red.
        init = content[content.index(_SMOKE_INIT):][: len(_SMOKE_INIT)]

        # litclock-dev#773 moved the KEEP/revert dispatch OUT of the
        # `if [[ -x "$PYTHON" ]]` block, so the text between the probes and the
        # dispatch now contains that block's `else` arm and its closing `fi`.
        # Lifting it verbatim yields an `else` with no `if` — a bash syntax
        # error, which surfaces as "the gate did not keep the update" rather
        # than as anything about the span. Stitch the two halves instead.
        probes_end = content.index("\nelse\n", probe_at)
        dispatch_start = content.index('if [[ "$smoke_rc" -eq 0 ]]; then', content.index("\nfi\n", probes_end))
        end = content.index('update_status_failed_reverted "Smoke test failed', dispatch_start)
        end = content.index("fi\n", content.index("exit 1", end)) + len("fi\n")
        span = init + content[start:probes_end] + "\n" + content[dispatch_start:end]

        invocations = [
            ln for ln in span.splitlines() if '"$PYTHON" src/eink_display.py catalog-get' in ln
        ]
        assert len(invocations) == 2, (
            f"expected exactly 2 probe sites inside the lifted span; found {len(invocations)}. "
            "Counting the bare word 'catalog-get' would count the comments too. This is `==`, "
            "not `>=`: the stitch DISCARDS the text between the probes and the dispatch, so a "
            "third probe landing in that gap would drop out of every executed test below while "
            "all of them stayed green."
        )
        # litclock-dev#773 item 2 added a COUNT probe. It is held to the same
        # two contracts as the value probes below (pinned to English, bounded by
        # a timeout), so it joins `invocations` rather than getting a weaker
        # check of its own — an unpinned or unbounded probe is the same hazard
        # whichever subcommand it calls.
        count_invocations = [
            ln for ln in span.splitlines() if '"$PYTHON" src/eink_display.py catalog-count' in ln
        ]
        assert len(count_invocations) == 1, (
            f"expected exactly 1 catalog-count probe inside the lifted span; found "
            f"{len(count_invocations)}. Same `==` reasoning as above."
        )
        invocations += count_invocations
        # The same blind spot, asserted directly. Anything the stitch throws
        # away is invisible to every test in this class, so nothing that the
        # class exists to check may live there.
        discarded = content[probes_end:dispatch_start]
        discarded_code = "\n".join(
            ln for ln in discarded.splitlines() if not ln.lstrip().startswith("#")
        )
        for subcommand in ("catalog-get", "catalog-count"):
            assert subcommand not in discarded_code, (
                f"a {subcommand} probe sits in the region the stitch discards — it would be "
                "exercised by NO test in this class, and the pin/timeout assertions below "
                "could not see it"
            )
        # The discarded region is the `-x` block's else arm, which IS executed —
        # by TestAMissingInterpreterIsAFailureNotASkip, whose span starts at the
        # initialisers and runs past the dispatch. Pin that, so the gap can never
        # widen into text no harness runs.
        assert discarded in _full_gate_span(content), (
            "the text this stitch discards is no longer inside the span the missing-interpreter "
            "harness executes, so it is now run by NO test in this file"
        )
        # EVERY probe stays pinned. litclock-dev#772 proposed an unpinned one to
        # exercise the device's own language; it was built, measured, and
        # removed — get()'s `de -> en -> key` fallback is total, so a broken
        # translation serves English rather than raw keys and the probe could
        # not fail while English was intact. The gate's job is DELIVERY
        # integrity — that the checkout arrived intact — and delivery damage is
        # not language-selective, which is why English works as the canary.
        # (An earlier version of this comment claimed catalog_lint and the
        # Stage-3/4 gates cover the content side. They do not, for the general
        # case: Stage-3 diffs the other languages AGAINST English, and
        # catalog_lint iterates the keys PRESENT in each bundle. Two source
        # sweeps DO exist and were measured live — catalog_lint.rich_capable_keys
        # over the 13 t_rich/_rich sites, and
        # TestBashTripletParity::test_every_painted_prefix_is_in_this_table over
        # the five splash scripts' --catalog-prefix sites. Measured UNSWEPT: a
        # ghost key in a Jinja `t()` call and in an in-function
        # `_catalog_get()`. Either way it is not this gate's job; tracked on
        # litclock-dev#773.)
        for ln in invocations:
            assert "LITCLOCK_LANGUAGE=en" in ln, f"unpinned catalog probe (litclock-dev#763): {ln.strip()}"
            assert "timeout 30" in ln, f"unbounded catalog probe — a hang inside the OTA: {ln.strip()}"
        # Comment-STRIPPED, same as the sibling belts in _full_gate_span. These
        # two were left raw when those were fixed (/review): "git reset --hard"
        # occurs in a comment inside this span, so the raw assert was satisfied
        # by prose. It only looked red because the `discarded in
        # _full_gate_span(content)` check above fires first — an accidental
        # shadow, not a guard.
        span_executed = _executed_lines(span)
        assert "smoke_rc=1" in span_executed, "the failure arm must be inside the span, or nothing can go red"
        assert "git reset --hard" in span_executed, "the revert dispatch must be inside the span"
        return span

    def _fake_checkout(self, tmp_path):
        """A tree that IS a repo root as far as strings_catalog is concerned.

        `_REPO_ROOT` is `Path(__file__).resolve().parent.parent`, so a real copy
        of `src/` here makes the registry, the bundles and env.sh all resolve out
        of tmp_path. Copied, not symlinked — `resolve()` would follow a symlink
        straight back to the real checkout and the fixture would test nothing.
        """
        root = tmp_path / "checkout"
        (root / "languages" / "zz").mkdir(parents=True)
        (root / "src").mkdir()
        # The .py modules only, not copytree: src/ picks up runtime debris a
        # tree copy chokes on (an lgpio notify FIFO survives a crashed painter),
        # and catalog-get needs nothing else.
        modules = sorted(REPO_ROOT.glob("src/*.py"))
        assert any(m.name == "strings_catalog.py" for m in modules), "src/*.py glob found no catalog"
        for module in modules:
            shutil.copy2(module, root / "src" / module.name)
        shutil.copytree(REPO_ROOT / "languages" / "en", root / "languages" / "en")

        registry = json.loads((REPO_ROOT / "languages.json").read_text(encoding="utf-8"))
        registry["languages"]["zz"] = dict(
            registry["languages"]["en"],
            code="zz",
            native_name="Zzzz",
            status="active",
            strings="languages/zz/strings.json",
        )
        (root / "languages.json").write_text(json.dumps(registry), encoding="utf-8")

        # EVERY value differs from English, not just the probed keys. With only
        # those translated, a future probe added WITHOUT the pin would resolve
        # the English value out of the zz bundle and this class would stay green
        # on the exact regression it exists to catch.
        english = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
        (root / "languages" / "zz" / "strings.json").write_text(
            json.dumps({k: f"ZZ-{v}" for k, v in english.items()}), encoding="utf-8"
        )

        # The real mechanism: the OWNER's language lives in env.sh, and the
        # update unit passes no environment at all. Setting LITCLOCK_LANGUAGE in
        # the test's own env instead would let the pin "work" for the wrong
        # reason — the probe would inherit it rather than set it.
        (root / "env.sh").write_text("export LITCLOCK_LANGUAGE=zz\n", encoding="utf-8")
        return root

    @staticmethod
    def _clean_env(**overrides):
        """Every LITCLOCK_* var dropped, not just LITCLOCK_LANGUAGE.

        `_env_file_path()` honours LITCLOCK_ENV_FILE and then LITCLOCK_DIR
        BEFORE the repo-root fallback the fixture relies on. Measured: an
        ambient `LITCLOCK_DIR` left three of these tests passing vacuously,
        because zz never activated — the class stayed loud only by accident,
        through the fixture guard.
        """
        return {**{k: v for k, v in os.environ.items() if not k.startswith("LITCLOCK_")}, **overrides}

    def _english(self, root, key):
        strings = json.loads((root / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
        return strings[key]

    def _drop_english_keys(self, root, *keys):
        bundle = root / "languages" / "en" / "strings.json"
        strings = json.loads(bundle.read_text(encoding="utf-8"))
        for key in keys:
            assert strings.pop(key, None) is not None, f"{key} was not in the English bundle to begin with"
        bundle.write_text(json.dumps(strings), encoding="utf-8")

    def _catalog_get(self, root, key, **overrides):
        return subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-get", key],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._clean_env(**overrides),
        ).stdout.strip()

    def _run_gate(self, root, span):
        """Execute the lifted span. Returns (result, probes_in_order)."""
        probe_log = root / "probes.log"
        wrapper = root / "python-probe-wrapper"
        wrapper.write_text(
            "#!/bin/bash\n"
            f'printf "%s\\n" "${{@: -1}}" >> {shlex.quote(str(probe_log))}\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        _harness_marker = root / ".runtime-render-validated"
        _harness_marker.write_text("harness")
        program = (
            "set -u\n"
            'GREEN=""\nRED=""\nYELLOW=""\nNC=""\n'
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            # The revert arm is real code with real side effects. Stub every one
            # of them AND run from the fake checkout (not a git repo), so even an
            # unstubbed git could not touch the developer's tree.
            'git() { echo "STUB_GIT $*"; }\n'
            'sudo() { echo "STUB_SUDO $*"; }\n'
            'atomic_remove_file() { echo "STUB_ATOMIC_REMOVE $1"; }\n'
            'atomic_write_file() { echo "STUB_ATOMIC_WRITE $1"; }\n'
            'update_status_failed_reverted() { echo "STUB_STATUS_REVERTED $1"; }\n'
            # Both terminal statuses stubbed, DISTINGUISHABLY. Stubbing only
            # one is the litclock-dev#764 blind spot: these harnesses omit
            # `set -e`, so the unstubbed call would emit `command not found`
            # to stderr and execution would carry on green.
            'update_status_failed_unrecovered() { echo "STUB_STATUS_UNRECOVERED $1"; }\n'
            # litclock-dev#845 — both revert arms now re-install the clock units
            # from the reverted tree before restarting them. Stubbed like every
            # other side effect; executed on its own in
            # TestRevertArmsReinstallTheClockUnits below.
            '_reinstall_clock_units_from_tree() { echo "STUB_REINSTALL_CLOCK_UNITS"; }\n'
            '_block_reverted_release() { echo "STUB_BLOCK_REVERTED $*"; }\n'
            f"PYTHON={shlex.quote(str(wrapper))}\n"
            "REVERT_SHA=deadbeef\nUPDATE_FAILED_FILE=/dev/null\nHASH_FILE=/dev/null\n"
            # litclock-dev#531 — the KEEP arm now re-stamps the runtime-render
            # marker when it is ABSENT. This harness runs under `set -u` and the
            # variable is defined ~500 lines above the lifted span, so without
            # this line bash dies at the guard and never reaches REACHED_END.
            # A REAL regular file, created below, so the guard sees a present
            # marker and skips the block — these tests are about the catalog
            # probes; the stamp block is executed on its own in
            # tests/test_runtime_render_autostamp.py. NOT /dev/null: the guard is
            # `! -f`, and /dev/null is a character device, so `-f` is false and
            # the block ran anyway, logging '/dev/null' as a fifth probe. Caught
            # by running it.
            f"RUNTIME_MARKER={shlex.quote(str(_harness_marker))}\n"
            # litclock-dev#847 item 1 — a PRESENT marker clears the validation
            # memo, so the KEEP arm reads this variable on the path these tests
            # take. Under `set -u` it must exist; the stubbed atomic_remove_file
            # keeps it a log line.
            f"RUNTIME_VALIDATION_MEMO_FILE={shlex.quote(str(root / 'memo.json'))}\n"
            "_LITCLOCK_UPDATE_FINALIZED=0\n"
            f"{span}\n"
            'echo "REACHED_END rc=$smoke_rc"\n'
        )
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
            env=self._clean_env(),
        )
        probes = probe_log.read_text(encoding="utf-8").split() if probe_log.exists() else []
        return result, probes

    def _assert_kept(self, r):
        """The gate KEPT the update — the whole dispatch ran, not just the probes."""
        assert "REACHED_END rc=0" in r.stdout, f"the gate did not keep the update\n{r.stdout}\n{r.stderr}"
        assert r.returncode == 0, r.stderr
        assert "Smoke test passed" in r.stdout
        assert "STUB_GIT reset --hard" not in r.stdout, "a passing gate must not revert"

    def _assert_reverted(self, r):
        """The gate REVERTED — and reached the revert arm, rather than dying.

        `"Catalog smoke failed" in stdout` is not enough on its own: `break` ->
        `exit 1` inside the probe loop emits that line and then leaves, skipping
        `git reset --hard`, the HASH_FILE delete, the failure marker and the
        service restart. Measured green before this assertion existed.
        """
        assert "Smoke test failed" in r.stdout, f"the gate did not revert\n{r.stdout}\n{r.stderr}"
        assert "STUB_GIT reset --hard deadbeef" in r.stdout, (
            f"the gate reported failure but never reached the revert arm\n{r.stdout}"
        )
        assert "STUB_SUDO systemctl start litclock.service" in r.stdout, (
            "a reverting OTA must bring the clock back up on the old SHA"
        )
        assert r.returncode == 1, f"the revert arm exits 1; got {r.returncode}\n{r.stdout}"

    def test_the_fixture_really_does_create_the_failure_condition(self, tmp_path):
        """The harness gets its own test, because a fixture that quietly failed
        to activate `zz` would make every test below pass for the reason they
        always passed — and that is the exact defect under repair."""
        root = self._fake_checkout(tmp_path)
        for key in (self.STATUS_KEY, *self.SPLASH_KEYS):
            assert self._catalog_get(root, key) == f"ZZ-{self._english(root, key)}", (
                f"the fixture did not activate zz for {key} — without it this class asserts nothing"
            )
        # ...and the pin is what pulls it back to English.
        assert self._catalog_get(root, self.STATUS_KEY, LITCLOCK_LANGUAGE="en") == "just now"

    def test_the_gate_passes_on_a_non_english_device(self, update_sh_content, tmp_path):
        root = self._fake_checkout(tmp_path)
        r, probes = self._run_gate(root, self._gate_span(update_sh_content))
        assert "Catalog smoke passed" in r.stdout, (
            "the catalog smoke gate failed on a device whose owner picked a non-English "
            f"language. That reverts the OTA on every weekly tick.\n{r.stdout}\n{r.stderr}"
        )
        assert "Splash-triplet smoke passed" in r.stdout
        assert "Catalog size smoke passed" in r.stdout, (
            f"the litclock-dev#773 count probe must also pass here\n{r.stdout}\n{r.stderr}"
        )
        self._assert_kept(r)
        # The count probe logs as "catalog-count": the wrapper records the LAST
        # argument, and that subcommand takes none.
        assert probes == [self.STATUS_KEY, *self.SPLASH_KEYS, "catalog-count"], (
            f"all four probes must run on the happy path; got {probes}. Dropping an entry "
            "from the `for probe in ...` list is otherwise invisible."
        )

    def test_the_gate_still_fails_when_the_catalog_is_gone(self, update_sh_content, tmp_path):
        """The litclock-dev#532 failure the gate was written for: `languages/`
        absent, so even the English lookup degrades to the raw key."""
        root = self._fake_checkout(tmp_path)
        shutil.rmtree(root / "languages")
        r, _ = self._run_gate(root, self._gate_span(update_sh_content))
        assert "Catalog smoke failed" in r.stdout
        self._assert_reverted(r)

    def test_the_status_probe_has_its_own_teeth(self, update_sh_content, tmp_path):
        """With `languages/` deleted every probe fails, so neutering any single
        comparison stayed green (measured). Each probe needs a failure only it
        can catch — here, the status key alone."""
        root = self._fake_checkout(tmp_path)
        self._drop_english_keys(root, self.STATUS_KEY)
        r, probes = self._run_gate(root, self._gate_span(update_sh_content))
        assert f"catalog-get returned '{self.STATUS_KEY}'" in r.stdout, (
            f"the failure must name the degraded value\n{r.stdout}"
        )
        self._assert_reverted(r)
        assert probes == [self.STATUS_KEY], (
            "the splash-triplet block is gated on smoke_rc and must not run after the status "
            f"probe failed; probes actually run: {probes}. Asserting on the absence of the "
            '"Splash-triplet smoke passed" line proves nothing here — a trailing `&&` '
            "suppresses that line whether or not the block ran."
        )

    @pytest.mark.parametrize("index", (0, 1))
    def test_each_splash_probe_has_its_own_teeth(self, update_sh_content, tmp_path, index):
        """Parametrised over the two entries of the `for probe in ...` list.

        Dropping BOTH splash keys is not enough: the loop `break`s on the first
        mismatch, so the second entry is never probed on any failing path and
        deleting it from the list survived. This is the litclock-dev#532
        bulk-extraction case the loop exists for — status keys fine, recovery
        screens showing raw keys — and update.sh calls the firstboot one "the
        copy a stuck user is left staring at".
        """
        key = self.SPLASH_KEYS[index]
        root = self._fake_checkout(tmp_path)
        self._drop_english_keys(root, key)

        assert self._catalog_get(root, self.STATUS_KEY, LITCLOCK_LANGUAGE="en") == "just now", (
            "precondition: the status probe must still PASS, or this repeats the test above"
        )

        r, probes = self._run_gate(root, self._gate_span(update_sh_content))
        assert "Catalog smoke passed" in r.stdout, "the status probe was supposed to pass"
        assert f"catalog-get {key} returned" in r.stdout, (
            f"the failure must name {key}, or that probe-list entry is untested\n{r.stdout}"
        )
        assert "Splash-triplet smoke passed" not in r.stdout
        self._assert_reverted(r)
        assert probes == [self.STATUS_KEY, *self.SPLASH_KEYS[: index + 1]], (
            f"the loop must probe up to {key} and break there; got {probes}"
        )

    def test_the_pin_is_confined_to_the_smoke_gate(self):
        """The inverse property, and the one this class makes it easy to break:
        panel text must render in the OWNER'S language. Every other
        `catalog-get` caller — boot splash, first-boot, shutdown, bootcheck
        recovery — must stay unpinned, so copying this prefix onto one of them
        would force English panels on a translated device.
        """
        # A COMMAND PREFIX specifically — `LITCLOCK_LANGUAGE=x cmd ...` — not the
        # `export LITCLOCK_LANGUAGE=...` lines that seed env.sh, which are how
        # the owner's choice gets persisted in the first place.
        prefix = re.compile(r"(?<![-\w])LITCLOCK_LANGUAGE=\S*\s+\S")
        offenders = []
        for script in sorted(REPO_ROOT.glob("scripts/*.sh")):
            for lineno, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("export "):
                    continue
                if not prefix.search(line):
                    continue
                if script.name == "update.sh" and ("catalog-get" in line or "catalog-count" in line):
                    continue
                offenders.append(f"{script.name}:{lineno}: {stripped}")
        assert not offenders, (
            "LITCLOCK_LANGUAGE is pinned outside the update.sh smoke gate. Panel copy is "
            "deliberately rendered in the owner's language (litclock-dev#532); pinning it "
            "forces English on a translated device:\n" + "\n".join(offenders)
        )


class TestCatalogTruncationIsCaught:
    """litclock-dev#773 item 2 — the three value probes cannot see truncation.

    Measured in the issue: an `en` bundle cut from 438 keys to just the three
    the gate probes passes every value comparison green, with 435 strings gone
    and every other status and splash surface degraded to raw keys. `languages/`
    being ABSENT is caught; truncation was caught only if it happened to hit one
    of three keys — a coin flip that gets worse as litclock-dev#532's bulk extraction
    grows the surface.

    Inherits the sibling class's fixtures deliberately: the count probe must be
    driven through the same REAL lifted span, the same real `$PYTHON` wrapper
    and the same real resolver. A stub would test a model of the loader, and
    the loader's filtering (`_catalog` drops non-str values and `_`-prefixed
    keys) is exactly what the count has to measure.
    """

    # COMPOSITION, not inheritance (/review). Subclassing a `Test`-prefixed
    # class makes pytest re-collect and re-run all SEVEN of the parent's tests
    # under this class's name: 10 node IDs collected here, only 3 of them new.
    # Each parent test builds a fake checkout (every src/*.py plus a copytree of
    # languages/en) and spawns several subprocesses plus a bash gate run, so it
    # roughly doubled the cost of the most expensive class in this file for zero
    # added coverage — and a genuine parent failure reported twice under two
    # different class names, which muddies attribution.
    #
    # The harness methods are stateless, so borrowing one instance is enough.
    _harness = TestCatalogSmokeGateIsLanguageAgnostic()

    STATUS_KEY = TestCatalogSmokeGateIsLanguageAgnostic.STATUS_KEY
    SPLASH_KEYS = TestCatalogSmokeGateIsLanguageAgnostic.SPLASH_KEYS

    def __getattr__(self, name):
        """Delegate the `_`-prefixed harness helpers to the shared instance."""
        if name.startswith("_"):
            return getattr(self._harness, name)
        raise AttributeError(name)

    def _truncate_english_to(self, root, keys):
        path = root / "languages" / "en" / "strings.json"
        full = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({k: full[k] for k in keys}), encoding="utf-8")

    def test_a_bundle_truncated_to_the_probed_keys_is_now_caught(self, update_sh_content, tmp_path):
        """THE case the issue measured, reproduced end to end."""
        root = self._fake_checkout(tmp_path)
        probed = [self.STATUS_KEY, *self.SPLASH_KEYS]
        self._truncate_english_to(root, probed)

        # Precondition: every VALUE probe still passes. Without this the test
        # could go green because the status probe failed first, proving nothing
        # about the count probe.
        assert self._catalog_get(root, self.STATUS_KEY, LITCLOCK_LANGUAGE="en") == "just now"

        r, probes = self._run_gate(root, self._gate_span(update_sh_content))
        assert "Catalog smoke passed" in r.stdout, "the value probes were supposed to pass"
        assert "Splash-triplet smoke passed" in r.stdout, "the value probes were supposed to pass"
        assert "Catalog smoke failed: catalog-count returned" in r.stdout, (
            f"a bundle truncated to the 3 probed keys must fail the COUNT probe\n{r.stdout}"
        )
        self._assert_reverted(r)
        assert probes == [*probed, "catalog-count"], (
            f"the count probe must run after the value probes; got {probes}"
        )

    def test_the_count_probe_reports_zero_rather_than_dying_when_the_catalog_is_broken(self, tmp_path):
        """Same `always exit 0 with a value` contract as catalog-get, and for
        the same reason: the gate compares STDOUT, so a non-zero exit with empty
        stdout is indistinguishable from a dead interpreter. 0 is honest AND
        below any floor, so the gate fails closed either way."""
        root = self._fake_checkout(tmp_path)
        (root / "languages" / "en" / "strings.json").write_text("{ this is not json", encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-count"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._clean_env(LITCLOCK_LANGUAGE="en"),
        )
        assert r.returncode == 0, f"catalog-count must exit 0 by contract\n{r.stderr}"
        assert r.stdout.strip() == "0", r.stdout

    def test_the_count_measures_what_the_loader_sees_not_the_raw_file(self, tmp_path):
        """`_catalog` drops `_`-prefixed keys and non-string values, so a count
        taken off the raw JSON would be wrong by exactly those. The gate has to
        measure what the app will actually resolve."""
        root = self._fake_checkout(tmp_path)
        path = root / "languages" / "en" / "strings.json"
        full = json.loads(path.read_text(encoding="utf-8"))
        raw = len(full)
        loaded_expected = len([k for k, v in full.items() if isinstance(v, str) and not k.startswith("_")])
        assert loaded_expected < raw, "fixture premise: the en bundle carries at least one _-prefixed key"

        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-count"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._clean_env(LITCLOCK_LANGUAGE="en"),
        )
        assert int(r.stdout.strip()) == loaded_expected, r.stdout

    # ---- litclock-dev#844 item 2: which LANGUAGE the count measures ---------
    #
    # Every test above pins `LITCLOCK_LANGUAGE=en`, so a count that ignored
    # the language entirely — or read a different channel than the gate pins
    # — stayed green. The `--language` flag the issue named is gone
    # (litclock-dev#840: it never had a caller); the environment is the only
    # channel, for the gate and for the owner's language alike.

    def _count(self, root, **env) -> str:
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-count"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._clean_env(**env),
        )
        assert r.returncode == 0, f"catalog-count must exit 0 by contract\n{r.stderr}"
        return r.stdout.strip()

    def _drop_one_zz_key(self, root):
        bundle = root / "languages" / "zz" / "strings.json"
        strings = json.loads(bundle.read_text(encoding="utf-8"))
        assert strings.pop(self.STATUS_KEY, None) is not None
        bundle.write_text(json.dumps(strings), encoding="utf-8")

    def test_the_count_follows_the_language_the_environment_pins(self, tmp_path):
        """A zz bundle one key short must count exactly one fewer than en
        under `LITCLOCK_LANGUAGE=zz` — so the count is measuring the language
        it was told to, not English regardless."""
        root = self._fake_checkout(tmp_path)
        self._drop_one_zz_key(root)
        en = int(self._count(root, LITCLOCK_LANGUAGE="en"))
        zz = int(self._count(root, LITCLOCK_LANGUAGE="zz"))
        assert en > 0
        assert zz == en - 1, f"en={en} zz={zz}: the count did not follow LITCLOCK_LANGUAGE"

    def test_without_the_pin_the_count_is_the_owners_language(self, tmp_path):
        """The fixture's env.sh says zz. With nothing in the environment the
        count resolves the OWNER's language — which is exactly why the gate's
        `LITCLOCK_LANGUAGE=en` prefix exists (litclock-dev#763), and why dropping it
        would compare a translated device's bundle against the English floor."""
        root = self._fake_checkout(tmp_path)
        self._drop_one_zz_key(root)
        en = int(self._count(root, LITCLOCK_LANGUAGE="en"))
        assert int(self._count(root)) == en - 1

    def test_a_registered_language_whose_bundle_is_missing_counts_zero(self, tmp_path):
        """The 0 arm, through the CLI: an active registry entry whose bundle
        is gone loads nothing, and 0 is below any floor."""
        root = self._fake_checkout(tmp_path)
        (root / "languages" / "zz" / "strings.json").unlink()
        assert self._count(root, LITCLOCK_LANGUAGE="zz") == "0"

    def test_an_unknown_code_degrades_to_english_not_to_zero(self, tmp_path):
        """`active_language()` degrades a code the registry does not list to
        English, so a typo in the pin can never make the gate compare 0
        against the floor for the wrong reason; 0 is reserved for a bundle
        that genuinely cannot be served."""
        root = self._fake_checkout(tmp_path)
        en = self._count(root, LITCLOCK_LANGUAGE="en")
        assert int(en) > 0
        assert self._count(root, LITCLOCK_LANGUAGE="nope") == en

    def test_the_language_flag_is_gone(self, tmp_path):
        """litclock-dev#840: `--language` had no callers and a second channel
        is a second thing to keep pinned. argparse must refuse it."""
        root = self._fake_checkout(tmp_path)
        r = subprocess.run(
            [sys.executable, "src/eink_display.py", "catalog-count", "--language", "en"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            env=self._clean_env(LITCLOCK_LANGUAGE="en"),
        )
        assert r.returncode == 2, f"--language is accepted again (exit {r.returncode})\n{r.stderr}"
        assert "unrecognized arguments" in r.stderr


class TestCatalogFloorStaysHonest:
    """The floor in update.sh is a constant, so something has to stop it drifting.

    An expectation derived from the committed `strings.json` would be a no-op:
    the loader reads that same file, so the comparison is vacuous precisely when
    the file is the thing that is damaged. The number therefore has to travel
    with update.sh — and this is the other side of that bargain.
    """

    # Anchored to a whole line so a mention inside a comment or an error
    # string cannot satisfy it, but tolerant of indentation -- the
    # assignment sits inside the `-x "$PYTHON"` block.
    FLOOR_RE = re.compile(r"^[ \t]*CATALOG_MIN_KEYS=(\d+)[ \t]*$", re.MULTILINE)

    @staticmethod
    def _loaded_english_key_count() -> int:
        full = json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
        return len([k for k, v in full.items() if isinstance(v, str) and not k.startswith("_")])

    def test_the_floor_is_below_the_real_count(self, update_sh_content):
        m = self.FLOOR_RE.search(update_sh_content)
        assert m, "CATALOG_MIN_KEYS is gone from update.sh — the truncation probe has no expectation"
        floor = int(m.group(1))
        actual = self._loaded_english_key_count()
        assert floor <= actual, (
            f"CATALOG_MIN_KEYS={floor} exceeds the {actual} keys the English bundle actually "
            "loads, so the OTA smoke gate would revert every update on every device"
        )

    def test_the_floor_has_not_gone_slack_as_the_catalog_grew(self, update_sh_content):
        """A floor ages LOOSE, which is safe but eventually useless. litclock-dev#532's
        bulk extraction is actively growing this surface, so require the floor
        to stay within 75% of the real count — a growing catalog then forces a
        deliberate bump rather than letting the gate quietly stop catching
        anything short of near-total loss."""
        floor = int(self.FLOOR_RE.search(update_sh_content).group(1))
        actual = self._loaded_english_key_count()
        assert floor >= actual * 0.75, (
            f"CATALOG_MIN_KEYS={floor} is now under 75% of the {actual} keys the English bundle "
            f"loads. Raise it (roughly {int(actual * 0.9)}) so the truncation probe keeps teeth."
        )


class TestHandoffGuardIsInertOnAnArrivingPreGuardUpgrade:
    """litclock-dev#768 — the litclock-dev#675 timezone refusal cannot arm when the
    OUTGOING script already wrote the marker, and the v0.225.0 CHANGELOG claimed
    otherwise.

    update.sh re-execs itself AFTER pulling the new code, so the outgoing
    script's migration runs first, in a different process, before any byte of
    the incoming file. In [v0.214.0, v0.225.0) that block is unconditional.

    The point is not that the ordering should change — it cannot, short of
    revoking a marker on a fielded device — but that the fact the correction
    rests on is load-bearing and easy to lose.
    """

    @staticmethod
    def _executed(body: str) -> list[str]:
        """Full-line comments dropped.

        Needed because this file now DESCRIBES the re-exec and the migration in
        prose right beside them; a raw search finds the explanation instead of
        the code. Inline trailing comments are left alone — none of the
        landmarks below appear in one, and stripping them properly would mean
        parsing quotes.
        """
        return [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]

    def test_the_migration_still_runs_before_the_re_exec_at_top_level(self, update_sh_content):
        """Ordering AND nesting, because ordering alone is not the property.

        /review measured the file-position version blind to the one refactor
        that genuinely inverts this: wrap the migration in
        `_handoff_migration() { ... }` left at the same offsets and call it
        after the re-exec. That is `bash -n` clean, really does move the write
        past the re-exec, and every test in this file passed. litclock-dev#719 already
        applied exactly that hoist to reset-setup.sh for SIGHUP reasons, so it
        is a refactor this repo performs.

        A `{`-depth scan is what closes it: inside a function body the
        migration is no longer reached at that point in the script, whatever
        its byte offset says.
        """
        lines = self._executed(update_sh_content)

        depth, marks = 0, {}
        for i, ln in enumerate(lines):
            if "touch /etc/litclock/.handoff-complete" in ln and "migration" not in marks:
                marks["migration"] = (i, depth)
            if 'git reset --hard "$TARGET_SHA"' in ln and "reset" not in marks:
                marks["reset"] = (i, depth)
            if 'exec "$SELF_SCRIPT"' in ln and "reexec" not in marks:
                marks["reexec"] = (i, depth)
            depth += ln.count("{") - ln.count("}")

        for name in ("migration", "reset", "reexec"):
            assert name in marks, f"landmark {name!r} not found in update.sh"

        assert marks["migration"][0] < marks["reset"][0] < marks["reexec"][0], (
            "update.sh's handoff migration no longer runs before the git reset and the "
            f"self re-exec: { {k: v[0] for k, v in marks.items()} }. The litclock-dev#768 "
            "CHANGELOG correction is written against that ordering — if it changed "
            "deliberately, update the correction too"
        )
        assert marks["migration"][1] == 0, (
            "the handoff migration is nested inside a function (brace depth "
            f"{marks['migration'][1]}). Its byte offset still precedes the re-exec, but a "
            "definition is not a call — hoisting it into a function and invoking it later "
            "moves the write PAST the re-exec while every position-based assertion stays "
            "green. Measured (litclock-dev#719 applied this hoist to reset-setup.sh)"
        )


class TestAMissingInterpreterIsAFailureNotASkip:
    """litclock-dev#773(a) — the whole gate sits inside `if [[ -x "$PYTHON" ]]`.
    When that was false the dry run, all three pinned probes and the shape probe
    were skipped SILENTLY and the update walked on to Phase 5/7 reporting
    SUCCESS: a false green in the dangerous direction, firing exactly when the
    device is most likely broken, since Phase 4 has just built this venv.

    The dispatch had to move OUT of that block to fix it. Setting `smoke_rc=1`
    in an `else` arm alone would have been a no-op — nothing read the variable
    outside the block, so the update would still have proceeded. Caught before
    shipping; this test is what keeps it caught.
    """

    @staticmethod
    def _full_span(content: str) -> str:
        return _full_gate_span(content)

    def test_the_dispatch_is_outside_the_interpreter_check(self, update_sh_content):
        """Structural, and the reason: `smoke_rc` is not read anywhere after the
        gate, so an `else` arm that only sets it changes nothing."""
        executed = "\n".join(
            ln for ln in update_sh_content.splitlines() if not ln.lstrip().startswith("#")
        )
        gate = executed.index('if [[ -x "$PYTHON" ]]; then')
        dispatch = executed.index('if [[ "$smoke_rc" -eq 0 ]]; then\n    log_info "Smoke test passed"')
        # Column 0 == top level == outside the -x block.
        line_start = executed.rindex("\n", 0, dispatch) + 1
        assert executed[line_start] != " ", (
            "the KEEP/revert dispatch is indented, i.e. still inside the `-x $PYTHON` block. "
            "A missing interpreter then skips it entirely and the update reports success "
            "having verified nothing (litclock-dev#773)"
        )
        assert gate < dispatch

    def _run_missing_interpreter(self, update_sh_content, root, no_interpreter=True):
        """Drive the real lifted span with `$PYTHON` either absent or failing.

        `no_interpreter=True` points it at a path that does not exist — what a
        half-built venv leaves behind. `False` points it at an executable that
        exits non-zero, i.e. an ORDINARY smoke failure, which is the control for
        the terminal-status assertions.
        """
        import shlex as _shlex
        import subprocess

        span = self._full_span(update_sh_content)
        if no_interpreter:
            python = root / "venv" / "bin" / "python3"
        else:
            python = root / "failing-python3"
            python.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
            python.chmod(0o755)

        _harness_marker = root / ".runtime-render-validated"
        _harness_marker.write_text("harness")
        program = (
            "set -u\n"
            'GREEN=""\nRED=""\nYELLOW=""\nNC=""\n'
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            'git() { echo "STUB_GIT $*"; }\n'
            'sudo() { echo "STUB_SUDO $*"; }\n'
            'atomic_remove_file() { echo "STUB_ATOMIC_REMOVE $1"; }\n'
            'atomic_write_file() { echo "STUB_ATOMIC_WRITE $1"; }\n'
            'update_status_failed_reverted() { echo "STUB_STATUS_REVERTED $1"; }\n'
            # Both terminal statuses stubbed, DISTINGUISHABLY. Stubbing only
            # one is the litclock-dev#764 blind spot: these harnesses omit
            # `set -e`, so the unstubbed call would emit `command not found`
            # to stderr and execution would carry on green.
            'update_status_failed_unrecovered() { echo "STUB_STATUS_UNRECOVERED $1"; }\n'
            # litclock-dev#845 — both revert arms now re-install the clock units
            # from the reverted tree before restarting them. Stubbed like every
            # other side effect; executed on its own in
            # TestRevertArmsReinstallTheClockUnits below.
            '_reinstall_clock_units_from_tree() { echo "STUB_REINSTALL_CLOCK_UNITS"; }\n'
            '_block_reverted_release() { echo "STUB_BLOCK_REVERTED $*"; }\n'
            f"PYTHON={_shlex.quote(str(python))}\n"
            "REVERT_SHA=deadbeef\nUPDATE_FAILED_FILE=/dev/null\nHASH_FILE=/dev/null\n"
            # litclock-dev#531 — the KEEP arm now re-stamps the runtime-render
            # marker when it is ABSENT. This harness runs under `set -u` and the
            # variable is defined ~500 lines above the lifted span, so without
            # this line bash dies at the guard and never reaches REACHED_END.
            # A REAL regular file, created below, so the guard sees a present
            # marker and skips the block — these tests are about the catalog
            # probes; the stamp block is executed on its own in
            # tests/test_runtime_render_autostamp.py. NOT /dev/null: the guard is
            # `! -f`, and /dev/null is a character device, so `-f` is false and
            # the block ran anyway, logging '/dev/null' as a fifth probe. Caught
            # by running it.
            f"RUNTIME_MARKER={shlex.quote(str(_harness_marker))}\n"
            # litclock-dev#847 item 1 — a PRESENT marker clears the validation
            # memo, so the KEEP arm reads this variable on the path these tests
            # take. Under `set -u` it must exist; the stubbed atomic_remove_file
            # keeps it a log line.
            f"RUNTIME_VALIDATION_MEMO_FILE={shlex.quote(str(root / 'memo.json'))}\n"
            "_LITCLOCK_UPDATE_FINALIZED=0\n"
            f"{span}\n"
            'echo "REACHED_END rc=$smoke_rc"\n'
        )
        return subprocess.run(
            ["bash", "-c", program], cwd=root, capture_output=True, text=True, timeout=120,
            env={k: v for k, v in os.environ.items() if not k.startswith("LITCLOCK_")},
        )

    def test_a_missing_interpreter_reverts(self, update_sh_content, tmp_path):
        """Executed. `$PYTHON` points at a path that does not exist, which is
        what a half-built venv leaves behind."""
        f = TestCatalogSmokeGateIsLanguageAgnostic()
        root = f._fake_checkout(tmp_path)
        r = self._run_missing_interpreter(update_sh_content, root)

        assert "Smoke test SKIPPED" in r.stdout, (
            f"the missing interpreter was not reported\n{r.stdout}\n{r.stderr}"
        )
        assert "STUB_GIT reset --hard deadbeef" in r.stdout, (
            "a missing interpreter did not revert. Every check was skipped, so the update "
            f"would install and report SUCCESS having verified nothing.\n{r.stdout}"
        )
        assert r.returncode == 1, f"expected the revert arm's exit 1; got {r.returncode}\n{r.stdout}"
        assert "REACHED_END" not in r.stdout, "the revert arm must exit, not fall through"
        assert "command not found" not in r.stderr, (
            "something the revert arm calls is not stubbed. These harnesses omit `set -e`, so "
            f"that runs green while the call does nothing (litclock-dev#764):\n{r.stderr}"
        )

    def test_a_missing_interpreter_reports_unrecovered_not_reverted(self, update_sh_content, tmp_path):
        """The revert CANNOT restore this device, so the status must not say it did.

        `venv/` is not in git: `git reset --hard` returns the code and leaves the
        interpreter still missing, and `runtheclock.sh` sources
        `./venv/bin/activate`. `failed_reverted` renders "rolled back. Your clock
        is running normally." — false here. The pip-failure branch reasons the
        same way for the strictly weaker case of an INDETERMINATE venv; a missing
        interpreter is known broken.
        """
        f = TestCatalogSmokeGateIsLanguageAgnostic()
        root = f._fake_checkout(tmp_path)
        r = self._run_missing_interpreter(update_sh_content, root)

        assert "STUB_STATUS_UNRECOVERED" in r.stdout, (
            "the missing-interpreter path did not report failed_unrecovered. If it reported "
            f"failed_reverted the owner is told the clock is running while it is dead.\n{r.stdout}"
        )
        assert "STUB_STATUS_REVERTED" not in r.stdout, (
            f"both terminal statuses fired; exactly one must.\n{r.stdout}"
        )
        assert "clock NOT restored" in r.stdout, (
            f"the console banner still claims a clean rollback.\n{r.stdout}"
        )

    def test_a_genuine_probe_failure_still_reports_reverted(self, update_sh_content, tmp_path):
        """The control. Without it, `failed_unrecovered` unconditionally — which
        would alarm every ordinary smoke failure — passes the test above.
        """
        f = TestCatalogSmokeGateIsLanguageAgnostic()
        root = f._fake_checkout(tmp_path)
        r = self._run_missing_interpreter(update_sh_content, root, no_interpreter=False)

        assert "STUB_STATUS_REVERTED" in r.stdout, (
            "an ordinary smoke failure must still report failed_reverted — smoke runs after a "
            f"SUCCESSFUL pip install, so the revert really does restore a matching venv.\n{r.stdout}"
        )
        assert "STUB_STATUS_UNRECOVERED" not in r.stdout, (
            f"an ordinary smoke failure was escalated to 'manual recovery needed'.\n{r.stdout}"
        )



class TestTheExitTrapRearmsTheClock:
    """litclock-dev#835, EXECUTED — the first version of this class scanned the
    trap's non-comment lines and passed with the re-arm inside `if false`
    (review mutation). Now the lifted trap block runs under bash with sudo,
    timeout and the status stamp stubbed, and a TERM is delivered to the
    shell the way systemd delivers one.

    Two properties, both found by the v0.227.0 port review: a signal must
    TERMINATE the run (bash resumes an interrupted phase after a handler
    returns, and the old shared handler never exited — the update carried on
    into Phase 5-7 inside systemd's stop window), and the unfinalized EXIT
    path must re-arm litclock.timer before stamping, best-effort."""

    def _lifted(self, update_sh_content):
        start = update_sh_content.index("_LITCLOCK_UPDATE_FINALIZED=0\n_LITCLOCK_UPDATE_CLEANED=0")
        marker = "trap _litclock_update_on_signal TERM INT HUP\n"
        end = update_sh_content.index(marker, start) + len(marker)
        return update_sh_content[start:end]

    def _run(self, update_sh_content, *, finalized: int, signal: bool, sudo_fails: bool = False):
        program = (
            "set -u\n"
            'sudo() { [ "$SUDO_FAILS" = 1 ] && return 1; echo "STUB_SUDO $*"; }\n'
            'timeout() { [ "$1" = -k ] && shift 2; shift; "$@"; }\n'
            'update_status_failed_unrecovered() { echo STUB_STAMP; }\n'
            'atomic_write_file() { echo STUB_GRACE; }\n'
            "_PHASE3_ADDED_FILE=\nPOST_UPDATE_GRACE_FILE=/dev/null\n"
            f"SUDO_FAILS={1 if sudo_fails else 0}\n"
            f"{self._lifted(update_sh_content)}"
            f"_LITCLOCK_UPDATE_FINALIZED={finalized}\n"
            "_LITCLOCK_UPDATE_PHASE_INDEX=4\n"
            + ("kill -TERM $$\n" if signal else "")
            + "echo MUTATING_PHASE_CONTINUES\n"
            "exit 0\n"
        )
        return subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)

    def test_a_signal_terminates_the_run_and_the_exit_trap_rearms_then_stamps(self, update_sh_content):
        r = self._run(update_sh_content, finalized=0, signal=True)
        assert r.returncode == 143, (r.returncode, r.stdout, r.stderr)
        assert "MUTATING_PHASE_CONTINUES" not in r.stdout, (
            "bash resumed the interrupted phase after the signal handler — the handler must exit"
        )
        grace = r.stdout.find("STUB_GRACE")
        rearm = r.stdout.find("systemctl start --no-block litclock.timer")
        stamp = r.stdout.find("STUB_STAMP")
        assert rearm != -1, r.stdout
        assert stamp != -1, r.stdout
        assert grace != -1 and grace < rearm, (
            "re-touch the LKG grace marker BEFORE the painter can heartbeat, or the writer "
            "promotes a never-finalized HEAD (round-2 review, F3)"
        )
        assert rearm < stamp, "re-arm the clock first; the status stamp is best-effort"
        assert r.stdout.count("STUB_STAMP") == 1, "the EXIT trap must run exactly once"

    def test_a_finalized_exit_neither_rearms_nor_stamps(self, update_sh_content):
        r = self._run(update_sh_content, finalized=1, signal=False)
        assert r.returncode == 0
        assert "STUB_SUDO" not in r.stdout and "STUB_STAMP" not in r.stdout, r.stdout

    def test_the_stamp_survives_a_failed_rearm(self, update_sh_content):
        r = self._run(update_sh_content, finalized=0, signal=True, sudo_fails=True)
        assert r.returncode == 143
        assert "STUB_STAMP" in r.stdout, r.stdout

    def test_a_signal_during_cleanup_does_not_abort_it(self, update_sh_content):
        """Round-2 review, reproduced: a TERM landing while the EXIT trap is
        already running (systemd's timeout hitting during an ordinary
        `exit 1` cleanup) fired the exiting handler INSIDE the trap, and the
        nested exit does not restart EXIT processing — no re-arm completed,
        no stamp. The trap now ignores further signals for its duration."""
        program = (
            "set -u\n"
            # The re-arm itself delivers a TERM to the shell mid-cleanup.
            'sudo() { echo "STUB_SUDO $*"; kill -TERM $$; sleep 0.2; echo REARM_FINISHED; }\n'
            'timeout() { [ "$1" = -k ] && shift 2; shift; "$@"; }\n'
            'update_status_failed_unrecovered() { echo STUB_STAMP; }\n'
            'atomic_write_file() { echo STUB_GRACE; }\n'
            "_PHASE3_ADDED_FILE=\nPOST_UPDATE_GRACE_FILE=/dev/null\n"
            f"{self._lifted(update_sh_content)}"
            "_LITCLOCK_UPDATE_FINALIZED=0\n_LITCLOCK_UPDATE_PHASE_INDEX=4\n"
            "exit 1\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert "REARM_FINISHED" in r.stdout, r.stdout
        assert "STUB_STAMP" in r.stdout, r.stdout
        assert r.returncode == 1, "the original exit status is preserved; the ignored signal does not replace it"

    def test_the_reachability_gate_is_bounded_and_the_traps_follow_the_status_init(self, update_sh_content):
        """Round-2 review (Claude): on the re-exec path Phase 1 has already
        stopped litclock.timer by the time the new script reaches the
        `git ls-remote` gate, and the traps come after it. They must — the
        EXIT trap's stamp needs update_status_init, and initialising before
        the gate would stamp an offline tick as unrecovered — so the window
        is closed the other way: the gate is bounded."""
        executed = _executed_lines(update_sh_content)
        assert "_remote_reachable() {" in executed and "! _remote_reachable; then" in executed
        assert 'timeout -k 5 "$_LS_REMOTE_TIMEOUT_S" git ls-remote --exit-code origin' in executed, (
            "coreutils timeout, group-killing with KILL escalation — not a hand-rolled watchdog"
        )
        init_at = update_sh_content.index('update_status_init "$OLD_SHA"')
        trap_at = update_sh_content.index("trap _litclock_update_trap EXIT")
        stop_at = update_sh_content.index("systemctl stop litclock.timer")
        assert init_at < trap_at < stop_at, "traps after the status init, before Phase 1 stops the timer"

    def _shim(self, tmp_path, body):
        """A PATH shim for git: `timeout` execs the real command lookup, so a
        shell function would be bypassed."""
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        shim = bindir / "git"
        shim.write_text("#!/bin/bash\n" + body + "\n")
        shim.chmod(0o755)
        return bindir

    def _gate_block(self, update_sh_content):
        start = update_sh_content.index("_LS_REMOTE_TIMEOUT_S=")
        end = update_sh_content.index("\n}\n", update_sh_content.index("_remote_reachable() {", start)) + len("\n}\n")
        return update_sh_content[start:end]

    def _gate_statement(self, update_sh_content):
        """The `if` that consumes _remote_reachable: the first `if [[` after
        the function's closing brace, to its `fi`. Anchored on the function,
        not on the condition text, so a mutated condition still lifts and
        the test fails on behaviour rather than on a missing substring."""
        fn_end = update_sh_content.index("\n}\n", update_sh_content.index("_remote_reachable() {")) + len("\n}\n")
        start = update_sh_content.index("if [[", fn_end)
        end = update_sh_content.index("\nfi\n", start) + len("\nfi\n")
        stmt = update_sh_content[start:end]
        assert "_remote_reachable" in stmt.splitlines()[0], stmt
        return stmt

    @pytest.mark.parametrize("reexec", [True, False], ids=["re-exec", "fresh-run"])
    def test_an_unreachable_remote_only_exits_a_fresh_run(self, update_sh_content, tmp_path, reexec):
        """v0.227.0 port review (adversarial): on the re-exec path Phase 1 has
        already stopped litclock.timer and the traps are not armed yet, so a
        gate failure there `exit 1`s with the clock stopped and the status
        stuck at `running` until the next weekly tick. The outgoing script
        has just fetched and reset, so reachability is proven: the gate is
        skipped when $1 (the outgoing script's OLD_SHA) is set, and still
        exits an offline FRESH run, where nothing is stopped yet."""
        bindir = self._shim(tmp_path, "exit 128")
        argv = "set -- deadbeef\n" if reexec else ""
        program = (
            f"set -u\nexport PATH={bindir}:$PATH\nexport LITCLOCK_LS_REMOTE_TIMEOUT_S=2\n"
            'log_error() { echo "[ERROR] $1"; }\n'
            f"ROLLBACK_TARGET_FILE={tmp_path}/absent-rollback-target\n"
            + argv
            + self._gate_block(update_sh_content)
            + self._gate_statement(update_sh_content)
            + "echo REACHED_PAST_GATE\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        if reexec:
            assert r.returncode == 0 and "REACHED_PAST_GATE" in r.stdout, (r.stdout, r.stderr)
            assert "Cannot reach remote" not in r.stdout
        else:
            assert r.returncode == 1 and "Cannot reach remote" in r.stdout, (r.stdout, r.stderr)
            assert "REACHED_PAST_GATE" not in r.stdout

    def test_a_hung_ls_remote_is_cut_at_the_bound(self, update_sh_content, tmp_path):
        bindir = self._shim(tmp_path, "sleep 30")
        program = (
            f"set -u\nexport PATH={bindir}:$PATH\nexport LITCLOCK_LS_REMOTE_TIMEOUT_S=1\n"
            + self._gate_block(update_sh_content)
            + "t0=$SECONDS; _remote_reachable; rc=$?; echo \"rc=$rc elapsed=$((SECONDS-t0))\"\n"
            f"printf '#!/bin/bash\\nexit 0\\n' > {bindir}/git\n_remote_reachable; echo \"ok_rc=$?\"\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert "ok_rc=0" in r.stdout, (r.stdout, r.stderr)
        m = re.search(r"rc=(\d+) elapsed=(\d+)", r.stdout)
        assert m and int(m.group(1)) >= 124 and int(m.group(2)) <= 8, (r.stdout, r.stderr)

    def test_a_term_immune_probe_tree_is_killed_by_the_escalation(self, update_sh_content, tmp_path):
        """`timeout` without --foreground makes itself a process-group leader
        and signals the GROUP on expiry, TERM then (with -k) KILL — so a git
        that ignores TERM, and any helper in its group, still dies. Readiness
        is signalled after the trap, the marker is unique per call, and the
        survivor inspection must itself have run. What `timeout` does NOT do
        — kill a helper git left behind, whether git exited on its own or
        honoured the TERM while the helper ignored it — is stated on the
        function as a residual."""
        marker = f"2727.{int(time.time() * 1000) % 1_000_000:06d}"
        ready = tmp_path / "ready"
        bindir = self._shim(tmp_path, f"trap '' TERM; touch {ready}; sleep {marker}")
        program = (
            f"set -u\nexport PATH={bindir}:$PATH\nexport LITCLOCK_LS_REMOTE_TIMEOUT_S=1\n"
            + self._gate_block(update_sh_content)
            + "_remote_reachable; echo \"rc=$?\"\n"
            "sleep 0.5\n"
            # Anchored: an unanchored -f matches this very harness's command line.
            f'pgrep -f "^sleep {marker}$" >/dev/null; echo "pgrep_rc=$?"\n'
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert ready.exists(), (r.stdout, r.stderr)
        m = re.search(r"rc=(\d+)", r.stdout)
        assert m and int(m.group(1)) >= 124, (r.stdout, r.stderr)
        assert "pgrep_rc=1" in r.stdout, r.stdout  # 1 = no match; 0 = survivor; 2+ = pgrep itself failed

    def test_the_lock_keeps_its_inherited_descriptor_on_purpose(self, update_sh_content):
        """Rounds 8-10 of the litclock-dev#835 review: `--close` would stop a
        leaked descendant holding the lock, but under systemd's group-wide
        TERM the flock parent dies first and the script's signal cleanup then
        runs unlocked against a second updater (reproduced). The pre-existing
        inheritance stays; the lock design is the issue's follow-up."""
        executed = _executed_lines(update_sh_content)
        assert 'flock -n -E 75 "$LITCLOCK_UPDATE_LOCK_FILE" "$0" "$@"' in executed
        assert "--close" not in executed

    def test_cleanup_ignores_every_signal_the_handler_catches(self, update_sh_content):
        lifted = _executed_lines(self._lifted(update_sh_content))
        assert lifted.count("trap '' TERM INT HUP") == 2, (
            "both the cleanup and the signal handler must start by ignoring the three signals"
        )

    @pytest.mark.parametrize("nth_statement", [1, 2, 3, 5])
    def test_a_term_injected_inside_cleanup_never_loses_the_recovery(self, update_sh_content, nth_statement):
        """Round-5 review (Codex) delivered a TERM deterministically between
        two statements of the cleanup with a DEBUG-trap injection; round 6
        then showed the first version of this test never actually injected
        (it keyed on $BASH_COMMAND, which inside an EXIT trap still reads
        `exit 7`). This one keys on FUNCNAME: the N-th DEBUG event inside
        _litclock_update_cleanup fires the TERM — N=1 is before the mask (the
        handler must run the whole cleanup itself), N=2 is the old
        flag-before-mask window, later N are mid-work (must be ignored) — and
        it asserts the injection HAPPENED and that recovery ran exactly once.

        Honesty note: the reviewer's harness reproduced the LOSS with the old
        ordering; this one, on bash 5.2 with the TERM raised from inside a
        DEBUG trap inside the EXIT trap, sees bash complete the interrupted
        cleanup anyway, so the old ordering passes here too. The mask-first
        order is kept because it is correct by construction (nothing before
        the mask can be interrupted into a "done" state); this test guards
        delivery and single execution, not that specific ordering."""
        program = (
            "set -u\nset -T\n"
            'sudo() { echo "STUB_SUDO $*"; }\n'
            'timeout() { [ "$1" = -k ] && shift 2; shift; "$@"; }\n'
            'update_status_failed_unrecovered() { echo STUB_STAMP; }\n'
            'atomic_write_file() { echo STUB_GRACE; }\n'
            "_PHASE3_ADDED_FILE=\nPOST_UPDATE_GRACE_FILE=/dev/null\n"
            f"{self._lifted(update_sh_content)}"
            "_LITCLOCK_UPDATE_FINALIZED=0\n_LITCLOCK_UPDATE_PHASE_INDEX=4\n"
            "_N=0\n"
            "trap 'if [ \"${FUNCNAME[0]:-}\" = _litclock_update_cleanup ]; then _N=$((_N+1)); "
            f"if [ \"$_N\" = {nth_statement} ]; then echo INJECTED; kill -TERM $$; fi; fi' DEBUG\n"
            "exit 7\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert "INJECTED" in r.stdout, (nth_statement, r.stdout, r.stderr)
        assert r.stdout.count("systemctl start --no-block litclock.timer") == 1, (nth_statement, r.stdout)
        assert r.stdout.count("STUB_STAMP") == 1, (nth_statement, r.stdout)
        assert r.stdout.count("STUB_GRACE") == 1, (nth_statement, r.stdout)

    def test_the_signal_handler_cleans_up_itself_and_cleanup_is_idempotent(self, update_sh_content):
        """Round-3 review (Codex): `trap ''` at the top of the EXIT trap is not
        atomic with entering it (40/1000 stress runs lost both the re-arm and
        the stamp). The handler must not rely on EXIT being entered again:
        it cleans up itself, and the flag makes the EXIT pass a no-op — one
        re-arm, one stamp, even though both paths run."""
        r = self._run(update_sh_content, finalized=0, signal=True)
        assert r.returncode == 143
        assert r.stdout.count("STUB_STAMP") == 1 and r.stdout.count("systemctl start") == 1, r.stdout
        lifted = self._lifted(update_sh_content)
        handler = lifted[lifted.index("_litclock_update_on_signal() {"):]
        assert "_litclock_update_cleanup" in handler.split("exit 143")[0], (
            "the signal handler must run the cleanup BEFORE exiting, not leave it to the EXIT trap"
        )

    def test_the_rearm_is_bounded_and_non_interactive(self, update_sh_content):
        lifted = _executed_lines(self._lifted(update_sh_content))
        assert "timeout -k 5 10 sudo -n systemctl start --no-block litclock.timer" in lifted, (
            "a trap that can block on a wedged manager or a password prompt holds the stop job "
            "past TimeoutStopSec and loses the status stamp with it"
        )


class TestRevertArmsReinstallTheClockUnits:
    """litclock-dev#845, EXECUTED — both revert arms (Phase 4 pip failure,
    Phase 4.5 smoke failure) re-install litclock.service and litclock.timer
    from the tree they just reset to, and reload systemd, BEFORE they restart
    them. The unit copy loop is Phase 5, after both arms' `exit 1`, so in a
    bootcheck rollback that failed pip or smoke the :56 timer (litclock-dev#762) was
    left driving LKG code with no render lead: the previous minute's quote on
    every tick until a later update succeeded.

    The fake tree starts on the NEW release (installed units match it, as they
    do after the bricking tick's Phase 5); the git stub's `reset` rewrites the
    tree to the LKG. The sudo stub really copies, and at the instant it sees
    `systemctl start litclock.<unit>` it records what is INSTALLED — so the
    assertion is on the state the clock is restarted against, not on call
    order alone. Mutants that go red: the helper call dropped from either
    arm, the `sudo cp` dropped from the helper, the daemon-reload dropped,
    and the `cmp -s` gate dropped (the already-matching control below)."""

    NEW_TIMER = "[Timer]\nOnCalendar=*-*-* *:*:56\n"
    LKG_TIMER = "[Timer]\nOnCalendar=*-*-* *:*:00\n"
    NEW_SERVICE = "[Service]\nExecStart=/home/pi/litclock/scripts/runtheclock.sh\n# v2\n"
    LKG_SERVICE = "[Service]\nExecStart=/home/pi/litclock/scripts/runtheclock.sh\n"

    @staticmethod
    def _helper(content: str) -> str:
        start = content.index("# --- litclock-dev#845 clock-unit reinstall BEGIN ---")
        end = content.index("# --- litclock-dev#845 clock-unit reinstall END ---", start)
        helper = content[start:end]
        assert "_reinstall_clock_units_from_tree()" in _executed_lines(helper)
        return helper

    @staticmethod
    def _pip_arm(content: str) -> str:
        start = content.index('log_error "pip install failed')
        end = content.index("exit 1", start) + len("exit 1")
        return content[start:end]

    @staticmethod
    def _smoke_arm(content: str) -> str:
        start = content.index('log_error "Smoke test failed (exit $smoke_rc)')
        end = content.index('update_status_failed_reverted "Smoke test failed', start)
        end = content.index("exit 1", end) + len("exit 1")
        return content[start:end]

    def _run(self, update_sh_content, tmp_path, *, arm: str, already_matching: bool = False,
             cp_fails: bool = False):
        root = tmp_path / "tree"
        (root / "systemd").mkdir(parents=True)
        (root / "lkg").mkdir()
        units = tmp_path / "units"
        units.mkdir()
        lkg_timer = self.NEW_TIMER if already_matching else self.LKG_TIMER
        lkg_service = self.NEW_SERVICE if already_matching else self.LKG_SERVICE
        (root / "systemd" / "litclock.timer").write_text(self.NEW_TIMER)
        (root / "systemd" / "litclock.service").write_text(self.NEW_SERVICE)
        (units / "litclock.timer").write_text(self.NEW_TIMER)
        (units / "litclock.service").write_text(self.NEW_SERVICE)
        (root / "lkg" / "litclock.timer").write_text(lkg_timer)
        (root / "lkg" / "litclock.service").write_text(lkg_service)
        (root / "req").write_text("")
        (root / "hash").write_text("x")
        arm_text = self._pip_arm(update_sh_content) if arm == "pip" else self._smoke_arm(update_sh_content)
        q = shlex.quote
        program = (
            "set -u\n"
            'GREEN=""\nRED=""\nYELLOW=""\nNC=""\n'
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            f"INSTALL_DIR={q(str(root))}\nSYSTEMD_UNIT_DIR={q(str(units))}\n"
            # The reset really changes the tree: that is the whole point.
            'git() { if [ "$1" = reset ]; then cp "$INSTALL_DIR"/lkg/* "$INSTALL_DIR"/systemd/; fi; '
            'echo "STUB_GIT $*"; }\n'
            # cp really copies; every systemctl start of a clock unit snapshots
            # what is installed at that instant.
            f"CP_FAILS={1 if cp_fails else 0}\n"
            'sudo() {\n'
            '  case "$1" in\n'
            '    cp) [ "$CP_FAILS" = 1 ] && { echo "STUB_SUDO_CP_REFUSED $*"; return 1; };\n'
            '       cp "$2" "$3" && echo "STUB_SUDO $*";;\n'
            '    systemctl) echo "STUB_SUDO $*";\n'
            '      if [ "$2" = start ]; then\n'
            '        echo "INSTALLED_AT_START $3 $(tr "\\n" "|" < "$SYSTEMD_UNIT_DIR/$3")"; fi;;\n'
            '    *) echo "STUB_SUDO $*";;\n'
            '  esac\n'
            '}\n'
            'atomic_write_file() { echo "STUB_ATOMIC_WRITE $1"; }\n'
            'update_status_failed_reverted() { echo "STUB_STATUS_REVERTED $1"; }\n'
            'update_status_failed_unrecovered() { echo "STUB_STATUS_UNRECOVERED $1"; }\n'
            # litclock-dev#865 put a call on this arm and the harness asserts
            # nothing is "command not found". Stubbed, not lifted: this
            # class is about the litclock-dev#845 reinstall ordering, and
            # TestBlockedShaOnSmokeRevert executes the real helper.
            '_block_reverted_release() { echo "STUB_BLOCK_REVERTED $*"; }\n'
            f"REVERT_SHA=lkg0000\nREQUIREMENTS_FILTERED={q(str(root / 'req'))}\n"
            f"HASH_FILE={q(str(root / 'hash'))}\nUPDATE_FAILED_FILE=/dev/null\n"
            "smoke_rc=1\nsmoke_no_interpreter=0\n_LITCLOCK_UPDATE_FINALIZED=0\n"
            f"{self._helper(update_sh_content)}\n"
            f"{arm_text}\n"
            'echo "REACHED_END"\n'
        )
        r = subprocess.run(
            ["bash", "-c", program], cwd=root, capture_output=True, text=True, timeout=60,
            env={k: v for k, v in os.environ.items() if not k.startswith("LITCLOCK_")},
        )
        return r, units

    @staticmethod
    def _installed_at_start(stdout: str, unit: str) -> str:
        lines = [ln for ln in stdout.splitlines() if ln.startswith(f"INSTALLED_AT_START {unit} ")]
        assert len(lines) == 1, f"expected exactly one `systemctl start {unit}` from the arm\n{stdout}"
        return lines[0].split(" ", 2)[2].replace("|", "\n")

    @pytest.mark.parametrize("arm", ["pip", "smoke"])
    def test_the_arm_restarts_the_clock_against_the_reverted_units(self, update_sh_content, tmp_path, arm):
        r, units = self._run(update_sh_content, tmp_path, arm=arm)
        assert r.returncode == 1 and "REACHED_END" not in r.stdout, (r.returncode, r.stdout, r.stderr)
        assert "command not found" not in r.stderr, r.stderr
        assert "STUB_GIT reset --hard lkg0000" in r.stdout, "the arm must reset first"
        # The clock was restarted against the LKG's units, not the new release's.
        assert self._installed_at_start(r.stdout, "litclock.timer") == self.LKG_TIMER, (
            "litclock.timer was started while the installed copy still carried the NEW "
            f"release's OnCalendar — the :56 timer against un-offset code (litclock-dev#845)\n{r.stdout}"
        )
        assert self._installed_at_start(r.stdout, "litclock.service") == self.LKG_SERVICE, r.stdout
        # And systemd was told, between the copy and the start.
        reset_at = r.stdout.index("STUB_GIT reset --hard")
        cp_at = r.stdout.index("STUB_SUDO cp ")
        reload_at = r.stdout.index("STUB_SUDO systemctl daemon-reload")
        start_at = r.stdout.index("STUB_SUDO systemctl start litclock.timer")
        assert reset_at < cp_at < reload_at < start_at, (
            f"order must be reset -> cp -> daemon-reload -> start; got\n{r.stdout}"
        )
        assert (units / "litclock.timer").read_text() == self.LKG_TIMER

    @pytest.mark.parametrize("arm", ["pip", "smoke"])
    def test_a_normal_failed_update_is_a_no_op_by_construction(self, update_sh_content, tmp_path, arm):
        """The reason the re-install runs ALWAYS rather than only in
        ROLLBACK_MODE: on a normal arm the installed units already match the
        tree being reverted to, and the `cmp -s` gate then copies nothing and
        reloads nothing. Drop the gate and this goes red."""
        r, _ = self._run(update_sh_content, tmp_path, arm=arm, already_matching=True)
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "STUB_SUDO cp " not in r.stdout, f"matching units must not be re-copied\n{r.stdout}"
        assert "daemon-reload" not in r.stdout, f"no copy, no reload\n{r.stdout}"
        assert self._installed_at_start(r.stdout, "litclock.timer") == self.NEW_TIMER

    @pytest.mark.parametrize("arm", ["pip", "smoke"])
    def test_each_copy_is_reloaded_before_the_next_one_starts(self, update_sh_content, tmp_path, arm):
        """litclock-dev#854 review, P1 — the reload used to run once after the loop, so a
        TERM landing between the timer copy and that reload sent the EXIT
        trap's re-arm into systemd's PREVIOUSLY loaded definition: the window
        this helper exists to close, reopened a few lines wide. One reload per
        successful copy removes it."""
        r, _ = self._run(update_sh_content, tmp_path, arm=arm)
        events = [
            ln for ln in r.stdout.splitlines()
            if ln.startswith("STUB_SUDO cp ") or ln == "STUB_SUDO systemctl daemon-reload"
        ]
        assert len(events) == 4, f"expected copy,reload,copy,reload; got {events}"
        assert [e.startswith("STUB_SUDO cp ") for e in events] == [True, False, True, False], (
            f"every copy must be followed by its own daemon-reload before the next copy\n{events}"
        )

    @pytest.mark.parametrize("arm", ["pip", "smoke"])
    def test_a_refused_copy_is_loud_and_does_not_change_the_verdict(self, update_sh_content, tmp_path, arm):
        """litclock-dev#854 review — `sudo cp` into /etc/systemd/system is granted only by
        010_pi-nopasswd (020 must NOT carry it: pi owns the source tree, so
        such a grant is root; 010 itself is kept — the litclock-dev#387/litclock-dev#82 drop was
        reversed 2026-07-12). Where the blanket grant is absent, or /etc is
        read-only, the re-install fails — and that must be visible rather than
        silent. The
        verdict stays what the arm decided: a revert that restored the CODE is
        still a revert, and escalating it helps nobody."""
        r, units = self._run(update_sh_content, tmp_path, arm=arm, cp_fails=True)
        assert "STUB_SUDO_CP_REFUSED" in r.stdout, r.stdout
        assert "do NOT match the reverted tree" in r.stdout, (
            f"a failed re-install was silent — the clock is restarted against a mismatched unit\n{r.stdout}"
        )
        assert r.returncode == 1
        expected = "STUB_STATUS_UNRECOVERED" if arm == "pip" else "STUB_STATUS_REVERTED"
        assert expected in r.stdout, f"the terminal status changed because of the re-install\n{r.stdout}"
        # And the clock was still restarted — a mismatched unit beats a dark panel.
        assert self._installed_at_start(r.stdout, "litclock.timer") == self.NEW_TIMER
        assert (units / "litclock.timer").read_text() == self.NEW_TIMER

    def test_the_helper_states_whose_privilege_it_rides_on(self, update_sh_content):
        """litclock-dev#854 review — the one thing a reader must not conclude from "this
        needs sudo cp" is "add it to 020". Pinned on the comment because the
        comment IS the deliverable here; the behaviour half is the test above."""
        helper = self._helper(update_sh_content)
        assert "010_pi-nopasswd" in helper and "020" in helper
        assert "litclock-set-timezone" in helper, (
            "the comment must name the root-owned-helper shape that replaces the grant, "
            "not leave the next reader to invent a sudoers line"
        )

    def test_both_arms_call_the_one_helper_before_their_starts(self, update_sh_content):
        """Structural pin on the executed lines: the call sits in each arm,
        after its reset and before its first `systemctl start`."""
        for arm in (self._pip_arm(update_sh_content), self._smoke_arm(update_sh_content)):
            executed = _executed_lines(arm)
            reset_at = executed.index('git reset --hard "$REVERT_SHA"')
            call_at = executed.index("_reinstall_clock_units_from_tree")
            start_at = executed.index("systemctl start litclock.service")
            assert reset_at < call_at < start_at, executed
            assert "log_error" in executed[call_at:start_at], (
                "a failed re-install must be logged loudly at the call site (litclock-dev#854 review)"
            )


class TestAnOfflineTickFinalizesCleanly:
    """litclock-dev#847 item 4, EXECUTED — the graceful-offline branch (no
    blessed Release SHA resolvable) exited 0 without finalizing, so the EXIT
    trap stamped `failed_unrecovered` and the PWA raised "manual recovery
    needed" for an ordinary offline tick. The branch now finalizes the way
    the blocked-sha branch always did: `complete`, timer re-armed, exit 0.

    Runs the lifted branch AND the lifted trap block against the REAL
    scripts/lib/update_status.sh writing to a tmp status file, so the
    assertion is on the JSON the PWA would read. Mutant: drop the two
    finalizing lines -> state is failed_unrecovered -> red."""

    @staticmethod
    def _offline_block(content: str) -> str:
        start = content.index('if [[ -z "$TARGET_SHA" ]]; then')
        end = content.index("# Row 2 of the D3 phase reading-list", start)
        block = content[start:end]
        assert "Could not resolve a blessed Release SHA" in block
        return block

    @staticmethod
    def _trap_block(content: str) -> str:
        start = content.index("_LITCLOCK_UPDATE_FINALIZED=0\n_LITCLOCK_UPDATE_CLEANED=0")
        marker = "trap _litclock_update_on_signal TERM INT HUP\n"
        end = content.index(marker, start) + len(marker)
        return content[start:end]

    def _run(self, update_sh_content, tmp_path, *, lib_sourced: bool = True, start_fails: bool = False):
        status = tmp_path / "update.status"
        lib = Path(__file__).resolve().parents[1] / "scripts" / "lib" / "update_status.sh"
        # `start_fails` fails ONLY the branch's own blocking re-arm; the trap's
        # bounded `--no-block` retry still succeeds, so the test can tell the
        # fallback ran rather than merely that something called sudo.
        program = (
            "set -u\n"
            f"export LITCLOCK_UPDATE_STATUS_FILE={shlex.quote(str(status))}\n"
            f". {shlex.quote(str(lib))}\n"
            'GREEN=""\nRED=""\nYELLOW=""\nNC=""\n'
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            f"START_FAILS={1 if start_fails else 0}\n"
            'sudo() { echo "STUB_SUDO $*";\n'
            '  case "$*" in *"--no-block"*) return 0;; esac\n'
            '  [ "$START_FAILS" = 1 ] && return 1; return 0; }\n'
            'timeout() { [ "$1" = -k ] && shift 2; shift; "$@"; }\n'
            'atomic_write_file() { :; }\n'
            "_PHASE3_ADDED_FILE=\nPOST_UPDATE_GRACE_FILE=/dev/null\n"
            + ("github_api_latest_release_tag() { :; }\n" if lib_sourced else "")
            + "update_status_init abc1234\nupdate_status_set_phase 1\n"
            f"{self._trap_block(update_sh_content)}"
            'TARGET_SHA=""\n'
            f"{self._offline_block(update_sh_content)}\n"
            'echo "REACHED_END"\n'
            "exit 0\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        data = json.loads(status.read_text()) if status.exists() else None
        return r, data

    def test_an_offline_tick_completes_rather_than_alarming(self, update_sh_content, tmp_path):
        if shutil.which("jq") is None:
            pytest.skip("update_status.sh writes through jq")
        r, data = self._run(update_sh_content, tmp_path)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "REACHED_END" not in r.stdout, "the offline branch must exit, not fall through to Phase 2"
        assert "STUB_SUDO systemctl start litclock.timer" in r.stdout, "the clock must be re-armed"
        assert data is not None, "the status file was never written"
        assert data["state"] != "failed_unrecovered", (
            "an ordinary offline tick was stamped as needing manual recovery — the EXIT trap "
            f"ran on an unfinalized exit (litclock-dev#847 item 4)\n{data}"
        )
        assert data["state"] == "complete", data
        assert data["to_version"] is None, "nothing was installed; the version must not move"

    def test_the_rearm_precedes_the_finalize(self, update_sh_content):
        """litclock-dev#854 review, P1 — the ordering, on the executed lines of BOTH no-op
        early-outs. Finalizing first disarms the EXIT trap while the clock is
        still stopped: a signal in that window leaves a stopped timer with
        neither the trap's fallback re-arm nor its stamp, which is strictly
        worse than the unfinalized behaviour this PR replaced."""
        executed = _executed_lines(update_sh_content)
        for anchor in ("Latest Release SHA $TARGET_SHA is blocked", "Could not resolve a blessed Release SHA"):
            arm_at = executed.index(anchor)
            end = executed.index("exit 0", arm_at)
            arm = executed[arm_at:end]
            start_at = arm.index("systemctl start litclock.timer")
            finalize_at = arm.index("update_status_complete")
            flag_at = arm.index("_LITCLOCK_UPDATE_FINALIZED=1")
            assert start_at < finalize_at < flag_at, (
                f"the re-arm must precede the finalize in the arm at {anchor!r}\n{arm}"
            )

    def test_a_failed_rearm_falls_through_to_the_trap(self, update_sh_content, tmp_path):
        """litclock-dev#854 review, P1 — a `systemctl start` that FAILS must not be
        finalized as a clean tick. The run stays unfinalized, the EXIT trap
        retries the re-arm (bounded, non-interactive) and stamps
        failed_unrecovered, which is the honest state for a clock that is not
        ticking."""
        if shutil.which("jq") is None:
            pytest.skip("update_status.sh writes through jq")
        r, data = self._run(update_sh_content, tmp_path, start_fails=True)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "could not re-arm litclock.timer" in r.stdout, r.stdout
        assert "systemctl start --no-block litclock.timer" in r.stdout, (
            f"the EXIT trap's fallback re-arm did not run — the clock stays stopped\n{r.stdout}"
        )
        assert data is not None and data["state"] == "failed_unrecovered", (
            f"a failed re-arm was reported as a clean tick\n{data}"
        )

    def test_the_legacy_no_lib_path_still_falls_through(self, update_sh_content, tmp_path):
        """The control: with no resolver sourced the branch is the legacy
        origin/master fallback and must NOT exit."""
        if shutil.which("jq") is None:
            pytest.skip("update_status.sh writes through jq")
        r, data = self._run(update_sh_content, tmp_path, lib_sourced=False)
        assert "REACHED_END" in r.stdout, (r.stdout, r.stderr)
        assert "legacy path" in r.stdout
